from __future__ import annotations

import asyncio
import contextlib

from loguru import logger

from app.config import get_settings
from app.services.mtproto_client import resolve_channel_entity
from app.utils.local_time import now as local_now

_LISTENER_TASK: asyncio.Task | None = None
_SNAPSHOTS: dict[tuple[int, int], set[tuple[int, str]]] = {}

def _emoji_key(rt) -> str | None:
    try:
        from telethon.tl.types import ReactionCustomEmoji, ReactionEmoji
    except Exception:
        return getattr(rt, "emoticon", None)
    if isinstance(rt, ReactionEmoji):
        return rt.emoticon
    if isinstance(rt, ReactionCustomEmoji):
        return "💎"
    return getattr(rt, "emoticon", None)

def _peer_uid(peer) -> int | None:
    try:
        from telethon.tl.types import PeerUser
    except Exception:
        return getattr(peer, "user_id", None)
    if isinstance(peer, PeerUser):
        return int(peer.user_id)
    return None

async def _resolve_author(chat_id: int, msg_id: int) -> int | None:
    """Автор сообщения: сначала реестр (Bot API id), затем MTProto-бэкфилл.

    Ключевые фиксы:
    1. Реакция на СОБСТВЕННОЕ сообщение тоже засчитывается («поставил») —
       раньше молча отбрасывалась.
    2. get_message_author возвращает id в формате Bot API (-100... для
       каналов); сравнение с from_uid идёт через _same_uid (нормализация),
       чтобы не было ложных «это свой же id».
    """
    from app.db.session import session_factory
    from app.services.access import numeric_chat_id
    from app.services.activity import ActivityService

    # Реестр сообщений ведётся в формате Bot API (-100XXXXXXXXXX), MTProto
    # отдаёт id без префикса — ищем по обеим нормализациям.
    variants = {int(chat_id)}
    norm = numeric_chat_id(int(chat_id))
    if norm is not None:
        variants.add(int(norm))
    async with session_factory() as session:
        repo = ActivityService(session).activity
        for cid in variants:
            to_user = await repo.get_message_author(cid, msg_id)
            if to_user is not None:
                return int(to_user)
    to_user = await _backfill_channel_post(chat_id, msg_id)
    return to_user


def _same_uid(a: int | None, b: int | None) -> bool:
    if a is None or b is None:
        return False
    try:
        from app.services.access import numeric_chat_id
        na, nb = numeric_chat_id(a), numeric_chat_id(b)
        return na is not None and na == nb
    except Exception:
        return int(a) == int(b)


async def _credit(from_uid: int, chat_id: int, msg_id: int, emoji: str) -> None:
    from app.db.session import session_factory
    from app.services.activity import ActivityService

    to_user = await _resolve_author(chat_id, msg_id)
    async with session_factory() as session:
        svc = ActivityService(session)
        if to_user is None:
            # Автора сообщения установить не удалось — засчитываем хотя бы
            # сам факт реакции («поставил»), иначе «реакцию ставлю, а
            # статистика не обновляется».
            to_user = from_uid
        credited = await svc.process_reaction(
            from_user=from_uid, to_user=to_user,
            chat_id=chat_id, message_id=msg_id, emoji=emoji,
        )
        await session.commit()
        if credited:
            logger.info(f"😀 MTProto-реакция зачтена: {from_uid}→{to_user} (msg {msg_id})")

async def _backfill_channel_post(chat_id: int, msg_id: int) -> int | None:
    try:
        from app.services.mtproto_client import holder
        client = await holder.get()
        msgs = await client.get_messages(int(chat_id), ids=[int(msg_id)])
        m = msgs[0] if msgs else None
        if m is None or not getattr(m, "sender_id", None):
            return None
        uid = int(m.sender_id)
        media_type = None
        has_media = False
        for attr in ("photo", "video", "voice", "document", "sticker"):
            if getattr(m, attr, None):
                media_type, has_media = attr, True
                break
        from app.db.session import session_factory
        from app.services.activity import ActivityService
        async with session_factory() as session:
            await ActivityService(session).log_message_only(
                user_id=uid, chat_id=int(chat_id), message_id=int(msg_id),
                text=m.message, has_media=has_media, media_type=media_type,
            )
            await session.commit()
        return uid
    except Exception as exc:
        logger.debug(f"MTProto backfill post {chat_id}:{msg_id} failed: {type(exc).__name__}")
        return None

def _tracked_ids() -> set[int]:
    """Отслеживаемые чаты в канонической Bot API форме (-100...).

    В настройках id могут быть записаны без знака (1004335857237) —
    abs() превращал их в положительный peer, который Telethon GetChannels
    отвергает с "Invalid channel object" / ChannelInvalidError. Нормализуем
    через numeric_chat_id: знак -100 восстанавливается для каналов, а
    обычные ЛС-id остаются отрицательными как есть.
    """
    from app.services.access import numeric_chat_id

    ids = get_settings().tracked_chat_ids
    out: set[int] = set()
    for i in ids or []:
        n = numeric_chat_id(i)
        out.add(int(n) if n is not None else int(i))
    return out

def _chat_id_of(peer) -> int | None:
    try:
        from telethon.tl.types import PeerChannel, PeerChat
    except Exception:
        return getattr(peer, "channel_id", None) and -(10**13 + int(peer.channel_id))
    if isinstance(peer, PeerChannel):
        return int(f"-100{int(peer.channel_id)}")
    if isinstance(peer, PeerChat):
        return -int(peer.chat_id)
    return None

def _snapshot_size() -> None:
    global _SNAPSHOTS
    if len(_SNAPSHOTS) > 4096:
        keep = list(_SNAPSHOTS.items())[len(_SNAPSHOTS) // 2:]
        _SNAPSHOTS = dict(keep)

async def handle_message_reactions(update) -> None:
    chat_id = _chat_id_of(getattr(update, "peer", None) or getattr(update, "peer_id", None))
    if chat_id is None:
        return
    tracked = _tracked_ids()
    if tracked and chat_id not in tracked:
        return

    current: set[tuple[int, str]] = set()
    for pr in (getattr(update, "reactions", None) or []):
        uid = _peer_uid(getattr(pr, "peer_id", None))
        key = _emoji_key(getattr(pr, "reaction", None))
        if uid is None or key is None:
            continue
        current.add((uid, key))

    msg_id = int(update.msg_id)
    snap_key = (chat_id, msg_id)
    prev = _SNAPSHOTS.get(snap_key)
    _SNAPSHOTS[snap_key] = current
    _snapshot_size()
    if prev is None:
        return
    added = current - prev
    for uid, emoji in sorted(added):
        with contextlib.suppress(Exception):
            await _credit(uid, chat_id, msg_id, emoji)

async def handle_bot_reaction(update) -> None:
    chat_id = _chat_id_of(getattr(update, "peer", None))
    if chat_id is None:
        return
    tracked = _tracked_ids()
    if tracked and chat_id not in tracked:
        return
    actor = _peer_uid(getattr(update, "actor", None))
    if actor is None:
        return
    old = {_emoji_key(r) for r in (update.old_reactions or [])} - {None}
    new = {_emoji_key(r) for r in (update.new_reactions or [])} - {None}
    for emoji in sorted(new - old):
        with contextlib.suppress(Exception):
            await _credit(actor, chat_id, int(update.msg_id), emoji)

async def _on_raw_update(event) -> None:
    try:
        from telethon.tl.types import UpdateBotMessageReaction, UpdateMessageReactions
    except Exception:
        return
    update = event
    if isinstance(update, UpdateMessageReactions):
        with contextlib.suppress(Exception):
            await handle_message_reactions(update)
    elif isinstance(update, UpdateBotMessageReaction):
        with contextlib.suppress(Exception):
            await handle_bot_reaction(update)

_REACTION_TYPES: tuple[type, ...] | None = None

def _reaction_types() -> tuple[type, ...]:
    global _REACTION_TYPES
    if _REACTION_TYPES is None:
        from telethon.tl.types import UpdateBotMessageReaction, UpdateMessageReactions
        _REACTION_TYPES = (UpdateMessageReactions, UpdateBotMessageReaction)
    return _REACTION_TYPES

async def warm_snapshots(client, hours: int = 24) -> int:
    from datetime import timedelta
    tracked = _tracked_ids()
    if not tracked:
        return 0
    primed = 0
    for cid in sorted(tracked):
        try:
            entity = await resolve_channel_entity(cid)
            since = local_now() - timedelta(hours=hours)
            async for m in client.iter_messages(entity, offset_date=since,
                                                reverse=True):
                key = (cid, int(m.id))
                if key in _SNAPSHOTS:
                    continue
                snap: set[tuple[int, str]] = set()
                for cr in (getattr(m, "reactions", None) or []):
                    uid = getattr(cr, "sender_id", None) or getattr(cr, "user_id", None)
                    emoji = _emoji_key(getattr(cr, "emotion", None)
                                       or getattr(cr, "emoticon", None)) \
                        if hasattr(cr, "emotion") else getattr(cr, "emoticon", None)
                    if uid is not None and emoji:
                        snap.add((int(uid), emoji))
                _SNAPSHOTS[key] = snap
                primed += 1
        except Exception as exc:
            logger.debug(f"MTProto reactions prime {cid} failed: {type(exc).__name__}")
    _snapshot_size()
    return primed

async def start_reaction_listener() -> asyncio.Task | None:
    global _LISTENER_TASK
    from app.services.mtproto_client import (
        credentials_configured,
        holder,
        telethon_available,
    )
    if not telethon_available() or not credentials_configured():
        return None
    try:
        client = await holder.get()
    except Exception as exc:
        logger.warning(f"MTProto reaction listener не запущен: {type(exc).__name__}: {str(exc)[:160]}")
        return None
    from telethon import events

    @client.on(events.Raw(types=_reaction_types()))
    async def _on_raw(update) -> None:
        try:
            await _on_raw_update(update)
        except Exception as exc:
            logger.debug(f"MTProto reaction event error: {type(exc).__name__}: {str(exc)[:200]}")

    _LISTENER_TASK = asyncio.current_task()
    logger.info("😀 MTProto reaction listener активен (реакции на любые "
                "сообщения в отслеживаемых чатах засчитываются)")
    try:
        n = await warm_snapshots(client)
        logger.info(f"😀 MTProto reactions: baseline снапшотов на {n} сообщ.")
    except Exception as exc:
        logger.debug(f"MTProto reactions prime failed: {type(exc).__name__}: {str(exc)[:160]}")
    return _LISTENER_TASK

async def stop_reaction_listener() -> None:
    _SNAPSHOTS.clear()
