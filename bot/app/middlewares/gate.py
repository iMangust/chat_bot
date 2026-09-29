from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import CallbackQuery, Message, TelegramObject, User
from loguru import logger

from sqlalchemy import select

from app.config import get_settings
from app.services.access import numeric_chat_id, schedule_celebration

SUBSCRIBE_CACHE_SEC = 300
_NEG_TTL_SEC = 15
_GRANTED_KEY = "sub_granted"

_pos_cache: dict[Any, float] = {}
_neg_cache: dict[Any, float] = {}
_warned_no_admin: set[str] = set()
_warned_no_gating: set[str] = set()
_bg_tasks: set[asyncio.Task] = set()

gate_last_reason: dict[str, Any] = {}

class _SyntheticPrivateChat:
    type = ChatType.PRIVATE

def required_chats() -> list[tuple[str, str]]:
    from app.services.access import required_chats as _svc_required_chats
    chats = []
    for cid, uname in _svc_required_chats():
        n = numeric_chat_id(cid) if (cid and not uname) else None
        if n is not None:
            cid = f"-100{n}"
        chats.append((cid, uname))
    return chats

def subscribe_kb() -> "Any":
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    st = get_settings()
    rows = []
    ch = st.channel_username or ""
    if not ch:
        for cid, uname in required_chats():
            if uname:
                ch = uname
                break
            if cid.startswith("-100"):
                ch = f"+{cid[4:]}"
                break
    if ch:
        rows.append([InlineKeyboardButton(text=f"📢 Подписаться: t.me/{ch}",
                                          url=f"https://t.me/{ch}")])
    rows.append([InlineKeyboardButton(text="✅ Я подписался — проверить",
                                      callback_data="gate:check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def reset_subscribe_cache(user_id: int | None = None) -> None:
    if user_id is None:
        _pos_cache.clear()
        _neg_cache.clear()
    else:
        _pos_cache.pop(user_id, None)
        _neg_cache.pop(user_id, None)

async def _notify_admin(bot, text_key: str, uid: str, text: str) -> None:
    ids = get_settings().admin_ids
    if not ids:
        return
    try:
        await bot.send_message(ids[0], text)
    except Exception as exc:
        logger.warning("gate admin notice for {} failed to send: {}", uid, exc)

async def known_subscriber_in_required_chats(user_id: int) -> bool:
    ids = {numeric_chat_id(cid) for cid, _ in required_chats()}
    ids.discard(None)
    if not ids:
        return False
    try:
        from app.db.models import ChannelSubscriber
        from app.db.session import session_factory
        async with session_factory() as session:
            from sqlalchemy import select
            stmt = select(ChannelSubscriber.user_id,
                          ChannelSubscriber.chats).where(
                ChannelSubscriber.user_id == int(user_id)).execution_options(
                populate_existing=True)
            rows = (await session.execute(stmt)).all()
        for _uid, chats in rows:
            norm = {numeric_chat_id(c) for c in (chats or [])}
            norm.discard(None)
            if norm & ids:
                return True
        return False
    except Exception as exc:
        logger.debug("gate db-fallback failed for {}: {}", user_id, exc)
        return False

async def verify_membership(bot, user_id: int) -> bool | None:
    st = get_settings()
    if not (st.channel_username or st.channel_chat_id or st.tracked_chat_ids):
        return True
    chats = required_chats()
    if not chats:
        return True
    errored = False
    negative = False
    for cid, uname in chats:
        targets: list[str] = []
        if uname:
            targets.append("@" + uname.lstrip("@"))
        if cid:
            raw = str(cid).lstrip("@")
            if raw not in targets:
                targets.append(raw)
            n = numeric_chat_id(cid)
            if n is not None:
                full = f"-100{n}"
                if full not in targets:
                    targets.append(full)
        for target in targets:
            try:
                member = await bot.get_chat_member(target, user_id)
            except TelegramForbiddenError:
                errored = True
                continue
            except TelegramAPIError:
                errored = True
                continue
            status = getattr(member, "status", "")
            if status in ("member", "administrator", "creator"):
                return True
            negative = True
    if negative:
        return False
    if errored:
        return None
    return False

async def _api_membership_verdict(bot, user_id: int):
    has_negative = False
    for cid, uname in required_chats():
        targets: list[str] = []
        if uname:
            targets.append("@" + uname.lstrip("@"))
        if cid:
            raw = str(cid).lstrip("@")
            if raw not in targets:
                targets.append(raw)
            n = numeric_chat_id(cid)
            if n is not None:
                full = f"-100{n}"
                if full not in targets:
                    targets.append(full)
        for target in targets:
            try:
                member = await bot.get_chat_member(target, user_id)
            except TelegramForbiddenError:
                continue
            except TelegramAPIError as exc:
                if "chat not found" in str(exc).lower():
                    logger.warning("gate: chat {} not visible to the bot — it is "
                                   "NOT an @username channel/supergroup or the bot "
                                   "is not in it; fix CHANNEL_USERNAME/TRACKED_CHAT_IDS",
                                   target)
                    continue
                continue
            status = getattr(member, "status", "")
            if status in ("member", "administrator", "creator"):
                return True
            has_negative = True
    if has_negative:
        return False
    return None

async def _api_confirms_member(bot, user_id: int) -> bool | None:
    try:
        verdict = await _api_membership_verdict(bot, user_id)
    except Exception:
        return None
    if verdict is True:
        return True
    if verdict is False:
        logger.info("gate: registry row for {} ignored — Bot API confirms "
                    "not a member (stale/phantom record)", user_id)
        return False
    return None

async def known_subscriber_in_db(user_id: int, bot=None) -> bool:
    if bot is not None:
        confirmed = await _api_confirms_member(bot, user_id)
        if confirmed is True:
            return True
        if confirmed is False:
            logger.info("gate: registry for {} ignored — Bot API confirms not a member",
                        user_id)
            return False
    return await known_subscriber_in_required_chats(user_id)

async def known_subscriber_ids(user_ids: list[int] | set[int]) -> set[int]:
    wanted = {int(u) for u in user_ids if u}
    if not wanted:
        return set()
    return {u for u in wanted if await known_subscriber_in_required_chats(u)}

def _remember_membership(bot, user_id: int, cid: str, source: str) -> None:
    async def _run() -> None:
        try:
            n = numeric_chat_id(cid) if cid else None
            if n is None:
                return
            from app.db.repositories import SubscriberRepository
            from app.db.session import session_factory
            async with session_factory() as session:
                await SubscriberRepository(session).add_membership_sql(
                    user_id, n)
        except Exception as exc:
            logger.warning("gate: record membership {} ({}) failed: {}",
                           user_id, source, exc)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        _bg_tasks.add(t := loop.create_task(_run()))
        t.add_done_callback(_bg_tasks.discard)

async def clear_registry_membership(user_id: int) -> None:
    try:
        from app.db.models import ChannelSubscriber
        from app.db.session import session_factory
        async with session_factory() as session:
            row = (await session.execute(
                select(ChannelSubscriber).where(
                    ChannelSubscriber.user_id == int(user_id))
            )).scalar_one_or_none()
            if row is not None and (row.chats or []):
                row.chats = []
                await session.commit()
        reset_subscribe_cache(int(user_id))
    except Exception as exc:
        logger.debug("gate: clear registry for {} failed: {}", user_id, exc)

async def _live_scan(bot, user_id: int) -> bool:
    try:
        from app.handlers.access import ensure_registry_fresh
        await ensure_registry_fresh(bot, user_id)
    except Exception as exc:
        logger.warning("gate: live scan fallback failed for {}: {}", user_id, exc)
    return await known_subscriber_in_db(user_id, bot)

def _fmt_target(cid: str, uname: str) -> str:
    if uname:
        return "@" + uname.lstrip("@")
    return cid


def is_subscribed_cached(user_id: int) -> bool | None:
    now = time.monotonic()
    pos = _pos_cache.get(user_id)
    if pos is not None and pos > now:
        return True
    neg = _neg_cache.get(user_id)
    if neg is not None and neg > now:
        return False
    return None

async def _mtproto_status(target: str | int, user_id: int):
    try:
        from app.services.mtproto_client import get_chat_member_status
        st = await get_chat_member_status(target, user_id)
        if st is None:
            reason = ""
            with contextlib.suppress(Exception):
                from app.services.mtproto_client import last_scan_error
                reason = await last_scan_error()
            return ("silent", reason or "MTProto вернул None (нет клиента/прав/"
                                        "entity; точная причина в логе DEBUG)")
        return ("ok", st)
    except Exception as exc:
        return ("error", f"{type(exc).__name__}: {str(exc)[:120]}")

async def _registry_row_state(user_id: int) -> str:
    try:
        from app.db.models import ChannelSubscriber
        from app.db.session import session_factory
        async with session_factory() as s:
            row = await s.get(ChannelSubscriber, int(user_id))
        if row is None:
            return "NO ROW"
        return f"row exists, chats={list(row.chats or [])!r}"
    except Exception as exc:
        return f"read failed: {type(exc).__name__}: {str(exc)[:80]}"

async def is_channel_subscribed(bot, user_id: int) -> bool:
    chats = required_chats()
    if not chats:
        logger.error("subscription gate disabled: TRACKED_CHAT_IDS/CHANNEL_* are empty — "
                     "anyone can use the bot")
        _notify_no_gating(bot)
        return True
    now = time.monotonic()
    pos = _pos_cache.get(user_id)
    if pos is not None and pos > now:
        logger.debug("gate: allow {} (positive cache)", user_id)
        return True
    neg = _neg_cache.get(user_id)
    if neg is not None and neg > now:
        logger.debug("gate: deny {} (negative cache, TTL {}с)", user_id,
                     int(_NEG_TTL_SEC))
        return False
    logger.info("gate: checking subscription for {} in chats {}",
                user_id, [c[0] or c[1] for c in chats])
    api_status: dict[str, str] = {}
    negative_api = False
    for cid, uname in chats:
        targets: list[str] = []
        if uname:
            targets.append("@" + uname.lstrip("@"))
        if cid:
            raw = str(cid).lstrip("@")
            if raw not in targets:
                targets.append(raw)
            n = numeric_chat_id(cid)
            if n is not None:
                full = f"-100{n}"
                if full not in targets:
                    targets.append(full)
        for target in targets:
            uid_key = uname or target
            try:
                member = await bot.get_chat_member(target, user_id)
            except TelegramForbiddenError as exc:
                api_status[target] = f"FORBIDDEN ({str(exc)[:60]})"
                if uid_key not in _warned_no_admin:
                    logger.warning("cannot check subscription for {}: {} — fail-open",
                                   target, exc)
                _notify_no_admin(bot, uid_key)
                continue
            except TelegramAPIError as exc:
                msg = str(exc)
                if "chat not found" in msg.lower():
                    api_status[target] = "CHAT NOT FOUND (bad id/not a public channel/bot not inside)"
                    logger.error("gate: chat {} not found via Bot API — this id is "
                                 "NOT usable for subscription checks. Set CHANNEL_USERNAME "
                                 "(@name of the channel where the bot is an admin) or put "
                                 "the correct -100... id into TRACKED_CHAT_IDS/CHANNEL_CHAT_ID",
                                 target)
                    continue
                api_status[target] = f"API ERROR {type(exc).__name__}: {msg[:60]}"
                logger.warning("subscription check failed for {} ({}): skip chat",
                               target, exc)
                continue
            api_status[target] = getattr(member, "status", "?")
            if member.status in ("member", "administrator", "creator"):
                if uname:
                    _pos_cache[user_id] = time.monotonic() + SUBSCRIBE_CACHE_SEC
                _neg_cache.pop(user_id, None)
                _remember_membership(bot, user_id, cid, "bot-api")
                logger.info("gate: allow {} (Bot API '{}' in {})",
                            user_id, member.status, target)
                return True
            negative_api = True
    hard_errors = [t for t, v in api_status.items()
                   if str(v).startswith(("FORBIDDEN", "API ERROR"))]
    if hard_errors and not negative_api:
        logger.info("gate: allow {} (fail-open: API unavailable — {})",
                    user_id, api_status)
        return True
    if not chats:
        logger.info("gate: DENY {} — no usable required chats configured ({})",
                    user_id, api_status or "empty")
        _neg_cache[user_id] = time.monotonic() + _NEG_TTL_SEC
        return False
    if await _mtproto_and_scan_fallback(bot, user_id, chats, api_status):
        return True
    _neg_cache[user_id] = time.monotonic() + _NEG_TTL_SEC
    logger.warning(
        "gate: DENY {} — all sources silent | Bot API: {} | registry: {} "
        "| MTProto/live-scan: см. строки выше",
        user_id, api_status, await _registry_row_state(user_id))
    return False

async def _mtproto_and_scan_fallback(bot, user_id: int,
                                     chats: list[tuple[str, str]],
                                     api_status: dict[str, str] | None = None,
                                     ) -> bool:
    if await known_subscriber_in_db(user_id, bot):
        logger.info("gate: allow {} — not visible via Bot API (privacy?) "
                    "but present in channel_subscribers registry", user_id)
        _pos_cache[user_id] = time.monotonic() + SUBSCRIBE_CACHE_SEC
        return True
    mt_notes: list[str] = []
    for cid, uname in chats:
        mt_target = uname or cid
        s_mt = str(mt_target).lstrip("@")
        if not (s_mt.startswith("-100") or s_mt.lstrip("-").isdigit()):
            continue
        kind, st = await _mtproto_status(mt_target, user_id)
        if kind == "error":
            mt_notes.append(f"{mt_target}: ИСКЛЮЧЕНИЕ {st}")
            continue
        if kind == "silent":
            mt_notes.append(f"{mt_target}: молчит ({st})")
            continue
        if st in ("member", "administrator", "creator"):
            logger.info("gate: allow {} — visible in {} only via MTProto "
                        "(Bot API said left-ish — privacy?)",
                        user_id, mt_target)
            _pos_cache[user_id] = time.monotonic() + SUBSCRIBE_CACHE_SEC
            _neg_cache.pop(user_id, None)
            _remember_membership(bot, user_id, cid, "mtproto")
            return True
        mt_notes.append(f"{mt_target}: MTProto='{st}'")
    if mt_notes:
        logger.warning("gate: MTProto probe did not confirm {} ({}); "
                       "trying live participants scan",
                       user_id, "; ".join(mt_notes))
    if await _live_scan(bot, user_id):
        logger.info("gate: allow {} — found by live participants scan", user_id)
        _pos_cache[user_id] = time.monotonic() + SUBSCRIBE_CACHE_SEC
        return True
    with contextlib.suppress(Exception):
        from app.handlers.access import last_scan_stats
        from app.services.mtproto_client import chat_participants_count
        seen, total = last_scan_stats()
        if seen is not None:
            expect = None
            for cid, uname in chats:
                expect = await chat_participants_count(uname or cid)
                if expect:
                    break
            if expect and seen < max(2, expect // 2):
                logger.error(
                    "gate: ⚠️ живой скан собрал только {} из {} участник(ов) — "
                    "выборка Telethon неполная (PARTICIPANTS_TOO_LARGE/entity/"
                    "пагинация). Отказ может быть ЛОЖНЫМ: включите показ списка "
                    "участников в группе или проверьте, что MTProto-аккаунт "
                    "состоит в чатах", seen, expect)
    try:
        from app.handlers.access import last_scan_stats as _lss
        seen, total = _lss()
    except Exception:
        seen = total = None
    gate_last_reason.clear()
    gate_last_reason.update({
        "user_id": user_id,
        "api_status": dict(api_status or {}),
        "registry": await _registry_row_state(user_id),
        "mtproto": "; ".join(mt_notes) or "нет MTProto-проб (чат не числовой?)",
        "scan_error": await last_scan_error_safe(),
        "scan_seen": seen, "scan_total_in_chat": total,
    })
    return False

async def last_scan_error_safe() -> str:
    with contextlib.suppress(Exception):
        from app.services.mtproto_client import last_scan_error
        return await last_scan_error() or ""
    return ""

def _notify_no_admin(bot, uid: str) -> None:
    if uid in _warned_no_admin:
        return
    _warned_no_admin.add(uid)
    try:
        t = asyncio.ensure_future(_notify_admin(
            bot, "no_admin", uid,
            f"⚠️ Не могу проверять доступ ({uid}): бот должен быть "
            "администратором канала с правом «Добавлять администраторов» "
            "(Add Admins). Пока доступ работает в режиме разрешения (fail-open)."))
    except RuntimeError:
        _warned_no_admin.discard(uid)
        return
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)

def _notify_no_gating(bot) -> None:
    key = "no-gating"
    if key in _warned_no_gating:
        return
    _warned_no_gating.add(key)
    try:
        t = asyncio.ensure_future(_notify_admin(
            bot, "no_gating", key,
            "⚠️ Проверка подписки отключена: не заданы TRACKED_CHAT_IDS и "
            "CHANNEL_USERNAME/CHANNEL_CHAT_ID. Любой пользователь может "
            "взаимодействовать с ботом — настройте обязательные чаты."))
    except RuntimeError:
        _warned_no_gating.discard(key)
        return
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)

_ENTRY_COMMANDS = {"start", "help"}

def _is_entry_command(message: Message, bot_username: str = "") -> bool:
    text = message.text or ""
    if not text.startswith("/"):
        return False
    body = text[1:].split()[0]
    cmd, _, named = body.partition("@")
    cmd = cmd.lower()
    if cmd not in _ENTRY_COMMANDS:
        return False
    if named and bot_username:
        return named.lower().lstrip("@") == bot_username.lower().lstrip("@")
    return True

async def _resolve_bot_username(bot) -> str:
    name = getattr(bot, "username", None) or getattr(
        getattr(bot, "me", None), "username", None)
    if name:
        return name.lower().lstrip("@")
    key = id(bot)
    cached = _BOT_USERNAME_CACHE.get(key)
    if cached is not None:
        return cached
    resolved = ""
    try:
        me = await bot.get_me()
        if getattr(me, "username", None):
            resolved = me.username.lower().lstrip("@")
    except Exception as exc:
        logger.debug("gate: get_me failed (username unknown): {}", exc)
    _BOT_USERNAME_CACHE[key] = resolved
    return resolved

_BOT_USERNAME_CACHE: dict[int, str] = {}

def _addressed_to_this_bot(text: str, bot_username: str) -> bool:
    body = text[1:].split()[0] if text.startswith("/") else ""
    if "@" not in body:
        return True
    name = body.split("@", 1)[1].lower()
    return not bot_username or name == bot_username.lower().lstrip("@")

def _is_serviceable_group_message(event) -> bool:
    if not isinstance(event, Message):
        return False
    if event.new_chat_members or event.left_chat_member:
        return True
    if event.text and event.text.startswith("/"):
        return False
    return not (event.pinned_message or event.new_chat_title
                or event.new_chat_photo or event.delete_chat_photo)

def _target_user(event) -> User | None:
    if isinstance(event, CallbackQuery):
        return event.from_user
    if isinstance(event, Message):
        return event.from_user
    return None

class AccessGateMiddleware(BaseMiddleware):

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)

        chat = getattr(event, "chat", None) or getattr(
            getattr(event, "message", None), "chat", None)
        if chat is None and isinstance(event, CallbackQuery):
            chat = (getattr(event.from_user, "_private_chat", None)
                    or _SyntheticPrivateChat())
        if chat is None:
            return await handler(event, data)

        if chat.type != ChatType.PRIVATE:
            if not isinstance(event, Message):
                with contextlib.suppress(Exception):
                    await event.answer()
                return None
            if _is_serviceable_group_message(event):
                return await handler(event, data)
            return None

        user = _target_user(event)
        if user is None:
            return await handler(event, data)
        if user.is_bot:
            return None

        recheck = isinstance(event, CallbackQuery) and \
            getattr(event, "data", "") == "gate:check"
        was_locked = is_subscribed_cached(user.id) is False
        try:
            subscribed = await is_channel_subscribed(data["bot"], user.id)
            if not subscribed and recheck:
                verdict = await verify_membership(data["bot"], user.id)
                if verdict is True:
                    subscribed = True
                elif verdict is False:
                    await clear_registry_membership(user.id)
                    logger.info("gate: recheck DENY {} — Bot API says not a "
                                "member; registry membership cleared", user.id)
        except Exception as exc:
            logger.warning("subscription gate crashed for {}: {} — allow", user.id, exc)
            subscribed = True

        if subscribed and (was_locked or recheck):
            schedule_celebration(data["bot"], user.id, user.first_name or "")

        text = getattr(event, "text", None) or ""
        bot_name = await _resolve_bot_username(data["bot"]) \
            if text.startswith("/") else ""
        if isinstance(event, Message) and text.startswith("/") \
                and not _addressed_to_this_bot(text, bot_name):
            return await handler(event, data)

        exempt = isinstance(event, Message) and _is_entry_command(event, bot_name)
        if not exempt and not subscribed:
            ch = get_settings().channel_username
            link = f"t.me/{ch}" if ch else ""
            text_out = ["🔒 Взаимодействие с ботом недоступно:",
                        "ты не подписан ни на наш канал, ни на группу обсуждения."]
            if link:
                text_out.append(f"\n📢 Подпишись ({link}) — и возвращайся, я жду!")
            else:
                text_out.append("\n📢 Подпишись на канал/группу — и возвращайся, я жду!")
            text_out.append("После подписки нажми «Проверить» или отправь /start.")
            text = "\n".join(text_out)
            if isinstance(event, CallbackQuery):
                await event.answer("Сначала подпишись на канал 📢", show_alert=True)
                await event.message.edit_text(text, reply_markup=subscribe_kb())
            else:
                await event.answer(text, reply_markup=subscribe_kb())
            return None

        data[_GRANTED_KEY] = True
        return await handler(event, data)
