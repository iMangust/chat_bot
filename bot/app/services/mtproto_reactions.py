"""Реакции через полный Telegram API (MTProto) — v1.5.19.

Почему это нужно
================
Bot API доставляет боту апдейт ``message_reaction`` ТОЛЬКО на сообщения,
которые ОН САМ отправил в чат (или когда у бота есть особые условия).
Реакции пользователей на ЧУЖИЕ сообщения (посты канала, сообщения других
людей в группе) бот через Bot API не видит вообще — поэтому «реакции не
отслеживаются», хотя все права админа включены и allowed_updates настроен.

MTProto-аккаунт (user), состоящий в тех же чатах, получает серверные
события ``UpdateUserTyping…`` / ``updateNewMessage`` / и главное —
``UpdateBotMessageReaction`` (для сообщений бота) и реакции на любые
сообщения в виде сырых TL-update'ов ``UpdateMessageReactions`` (список
``MessagePeerReaction``: кто и какую реакцию поставил/снял).

Что делает модуль
=================
* слушает raw-события Telethon в отслеживаемых чатах;
* для ``UpdateMessageReactions`` (полный список реаций на сообщение с
  авторами) вычисляет НОВЫХ авторов реакций относительно прошлого
  снапшота и засчитывает их через ActivityService.process_reaction —
  ту же логику XP/антифрода/кэпов, что и Bot API-хендлер;
* автором сообщения считается владелец реакции «первого уровня»? Нет —
  автора берём из chat_messages_log (трекер пишет его для групповых
  сообщений). Для постов КЛАССА «канал» автор = сам канал, а реакция
  пользователя на пост канала засчитывается как реакция «в никуда» —
  чтобы не терять активность, считаем получателем автора поста, если он
  зарегистрирован (через backfill: MTProto видит и сообщения канала —
  см. log_message_only в ActivityService);
* дедупликация — уникальность ReactionLog (from_user, message_id, emoji)
  уже обеспечена репозиторием: повторный event ничего не начислит.

Безопасность: никаких исходящих запросов к Telegram от user-аккаунта,
кроме чтения live-событий; ошибки одного события не роняют listener.
"""
from __future__ import annotations

import asyncio
import contextlib

from loguru import logger

from app.config import get_settings
from app.services.mtproto_client import resolve_channel_entity
from app.utils.local_time import now as local_now

# Сырые TL-типы импортируются лениво (telethon опционален).
_LISTENER_TASK: asyncio.Task | None = None
# снапшот последних известных реаций: {(chat_id, msg_id): {(uid, emoji_key)}}
_SNAPSHOTS: dict[tuple[int, int], set[tuple[int, str]]] = {}


def _emoji_key(rt) -> str | None:
    """Ключ реакции из TL-объекта ReactionEmoji/ReactionCustomEmoji."""
    try:
        from telethon.tl.types import ReactionEmoji, ReactionCustomEmoji
    except Exception:  # noqa: BLE001
        return getattr(rt, "emoticon", None)
    if isinstance(rt, ReactionEmoji):
        return rt.emoticon
    if isinstance(rt, ReactionCustomEmoji):
        return "💎"  # premium-реакция — как в Bot API-хендлере
    return getattr(rt, "emoticon", None)


def _peer_uid(peer) -> int | None:
    """user_id из PeerUser/PeerChannel/PeerChat (каналы не считаем)."""
    try:
        from telethon.tl.types import PeerUser
    except Exception:  # noqa: BLE001
        return getattr(peer, "user_id", None)
    if isinstance(peer, PeerUser):
        return int(peer.user_id)
    return None  # реакция от имени канала/группы — персонального автора нет


async def _credit(from_uid: int, chat_id: int, msg_id: int, emoji: str) -> None:
    """Зачесть одну новую реакцию, используя общую логику ActivityService."""
    from app.db.session import session_factory
    from app.services.activity import ActivityService

    async with session_factory() as session:
        svc = ActivityService(session)
        repo = svc.activity
        to_user = await repo.get_message_author(chat_id, msg_id)
        if to_user is None or to_user == from_uid:
            # Автор сообщения неизвестен (бот его не видел) или само-реакция.
            # Для каналов попробуем подтянуть пост через MTProto один раз.
            if to_user is None:
                to_user = await _backfill_channel_post(chat_id, msg_id)
            if to_user is None or to_user == from_uid:
                return
        credited = await svc.process_reaction(
            from_user=from_uid, to_user=to_user,
            chat_id=chat_id, message_id=msg_id, emoji=emoji,
        )
        await session.commit()
        if credited:
            logger.info("😀 MTProto-реакция зачтена: {}→{} (msg {})",
                        from_uid, to_user, msg_id)


async def _backfill_channel_post(chat_id: int, msg_id: int) -> int | None:
    """Пост канала: получаем автора сообщения через MTProto и пишем в лог.

    Бот не получает published-сообщения каналов через Bot API, поэтому
    chat_messages_log пуст и реакция «некому». User-аккаунт может прочитать
    сообщение напрямую (get_messages) и определить автора (sender_id).
    """
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
    except Exception as exc:  # noqa: BLE001
        logger.debug("MTProto backfill post {}:{} failed: {}",
                     chat_id, msg_id, type(exc).__name__)
        return None


def _tracked_ids() -> set[int]:
    ids = get_settings().tracked_chat_ids
    return {abs(int(i)) for i in ids} if ids else set()


def _chat_id_of(peer) -> int | None:
    """Peer → отрицательный chat_id в нотации Bot API (-100… для каналов)."""
    try:
        from telethon.tl.types import PeerChannel, PeerChat
    except Exception:  # noqa: BLE001
        return getattr(peer, "channel_id", None) and -(10**13 + int(peer.channel_id))
    if isinstance(peer, PeerChannel):
        # -100xxxxxxxxxx как у Bot API: префикс «-100» + id канала.
        # Ошибка v1.5.19 (найдена тестом): -(10**13 + id) сдвигал на 14 цифр
        # и давал несовпадение с tracked_chat_ids ⇒ listener молчал.
        return int(f"-100{int(peer.channel_id)}")
    if isinstance(peer, PeerChat):
        return -int(peer.chat_id)
    return None


def _snapshot_size() -> None:
    """Гигиена памяти: снапшоты старых сообщений понемногу устаревают."""
    global _SNAPSHOTS
    if len(_SNAPSHOTS) > 4096:
        # отбрасываем половину произвольно — худшее последствие: повторная
        # реакция после eviction будет засчитана заново только если её сняли
        # и поставили снова (дедуп ReactionLog всё равно защитит от дабля)
        keep = list(_SNAPSHOTS.items())[len(_SNAPSHOTS) // 2:]
        _SNAPSHOTS = dict(keep)


async def handle_message_reactions(update) -> None:
    """UpdateMessageReactions: полный список реакций {кто, какую} на сообщение.

    Дельта считается относительно прошлого снапшота: начисляем только новым
    авторам. Первый sighting сообщения — только запоминаем (не начисляем
    задним числом за реакции, поставленные до нашего подключения).
    """
    chat_id = _chat_id_of(getattr(update, "peer", None) or getattr(update, "peer_id", None))
    if chat_id is None:
        return
    tracked = _tracked_ids()
    if tracked and abs(chat_id) not in tracked:
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
    """UpdateBotMessageReaction: дельта-событие о реакции на сообщение БОТА.

    Telegram шлёт его user-аккаунту, когда реагируют на посты бота в чатах
    (actor + old/new списки). Здесь данные уже дельта-типа — new - old.
    """
    chat_id = _chat_id_of(getattr(update, "peer", None))
    if chat_id is None:
        return
    tracked = _tracked_ids()
    if tracked and abs(chat_id) not in tracked:
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
    """Точка входа events.Raw: фильтруем нужные TL-апдейты по типу."""
    try:
        from telethon.tl.types import UpdateBotMessageReaction, UpdateMessageReactions
    except Exception:  # noqa: BLE001 — telethon не установлен
        return
    update = event  # Raw.build возвращает сам update
    if isinstance(update, UpdateMessageReactions):
        with contextlib.suppress(Exception):
            await handle_message_reactions(update)
    elif isinstance(update, UpdateBotMessageReaction):
        with contextlib.suppress(Exception):
            await handle_bot_reaction(update)


_REACTION_TYPES: tuple[type, ...] | None = None


def _reaction_types() -> tuple[type, ...]:
    """TL-типы реакций для фильтра events.Raw(types=...).

    ВАЖНО (баг v1.5.19): без types Telethon вызывает обработчик на КАЖДЫЙ
    сырой апдейт (сотни на минуту), а падение импорта типов молча глушило
    всю обработку через suppress(Exception). Здесь типы резолвятся явно и
    ошибка видна в логе.
    """
    global _REACTION_TYPES
    if _REACTION_TYPES is None:
        from telethon.tl.types import (UpdateBotMessageReaction,
                                       UpdateMessageReactions)
        _REACTION_TYPES = (UpdateMessageReactions, UpdateBotMessageReaction)
    return _REACTION_TYPES


async def warm_snapshots(client, hours: int = 24) -> int:
    """Prime снапшотов реакций на свежих сообщениях отслеживаемых чатов.

    Иначе первый же UpdateMessageReactions после рестарта бота — это
    «первый sighting» (prev=None), и реакции, поставленные ДО перезапуска,
    задним числом не засчитываются (правило anti-backfill). Но и новые
    реакции на таких сообщениях терялись до второго события. Prime решает:
    читаем последние сообщения, берём их current_reactions и запоминаем как
    baseline. Возвращает число сообщений со снапшотом.
    """
    from datetime import datetime, timedelta, timezone
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
                    # MessageReactions/ChatReactions: по одному author_id
                    # (last_viewers — не авторы, их игнорируем)
                    uid = getattr(cr, "sender_id", None) or getattr(cr, "user_id", None)
                    emoji = _emoji_key(getattr(cr, "emotion", None)
                                       or getattr(cr, "emoticon", None)) \
                        if hasattr(cr, "emotion") else getattr(cr, "emoticon", None)
                    if uid is not None and emoji:
                        snap.add((int(uid), emoji))
                _SNAPSHOTS[key] = snap
                primed += 1
        except Exception as exc:  # noqa: BLE001 — один чат не валит prime
            logger.debug("MTProto reactions prime {} failed: {}", cid,
                         type(exc).__name__)
    _snapshot_size()
    return primed


async def start_reaction_listener() -> asyncio.Task | None:
    """Подписаться на raw-события реакций. None — если MTProto не настроен."""
    global _LISTENER_TASK
    from app.services.mtproto_client import (credentials_configured,
                                             holder, telethon_available)
    if not telethon_available() or not credentials_configured():
        return None
    try:
        client = await holder.get()
    except Exception as exc:  # noqa: BLE001 — нет сессии: живём на Bot API
        logger.warning("MTProto reaction listener не запущен: {}: {}",
                       type(exc).__name__, str(exc)[:160])
        return None
    from telethon import events

    @client.on(events.Raw(types=_reaction_types()))
    async def _on_raw(update) -> None:  # Raw.build возвращает сам TL-update
        try:
            await _on_raw_update(update)
        except Exception as exc:  # noqa: BLE001 — логируем, не глотаем молча
            logger.debug("MTProto reaction event error: {}: {}",
                         type(exc).__name__, str(exc)[:200])

    _LISTENER_TASK = asyncio.current_task()
    logger.info("😀 MTProto reaction listener активен (реакции на любые "
                "сообщения в отслеживаемых чатах засчитываются)")
    # baseline снапшотов: не потерянные новые реакции и без backfill
    try:
        n = await warm_snapshots(client)
        logger.info("😀 MTProto reactions: baseline снапшотов на {} сообщ.", n)
    except Exception as exc:  # noqa: BLE001 — prime не должен валить листенер
        logger.debug("MTProto reactions prime failed: {}: {}",
                     type(exc).__name__, str(exc)[:160])
    return _LISTENER_TASK


async def stop_reaction_listener() -> None:
    # подписки живут на клиенте; при остановке клиента они снимаются сами
    _SNAPSHOTS.clear()
