from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Iterable

from loguru import logger

from app.config import get_settings

_celebrated: dict[int, float] = {}
_CELEBRATE_COOLDOWN_SEC = 3 * 3600
_celebrate_tasks: set[asyncio.Task] = set()

def _celebrate_throttled(user_id: int) -> bool:
    now = time.monotonic()
    last = _celebrated.get(int(user_id))
    if last is not None and now - last < _CELEBRATE_COOLDOWN_SEC:
        return True
    _celebrated[int(user_id)] = now
    return False

async def celebrate_subscription(bot, user_id: int, first_name: str = "") -> None:
    if _celebrate_throttled(user_id):
        return
    st = get_settings()
    uname = (st.channel_username or "").lstrip("@").strip()
    link = f"https://t.me/{uname}" if uname else None
    reward_xp, reward_coins = 50, 20
    try:
        from app.db.repositories import UserRepository
        from app.db.session import session_factory
        async with session_factory() as session:
            await UserRepository(session).add_xp_coins(user_id, reward_xp, reward_coins)
            await session.commit()
    except Exception as exc:
        logger.warning("celebrate: reward grant failed for {}: {}", user_id,
                       type(exc).__name__)
    text = (f"🎉 Добро пожаловать{', ' + first_name if first_name else ''}!\n\n"
            f"Подписка на канал подтверждена ✅\n"
            f"Начисляем бонус за вступление: <b>+{reward_xp} XP</b> и "
            f"<b>+{reward_coins} монет</b> 🪙\n\n")
    if link:
        text += f"Там свежие новости и анонсы: {link}\n\n"
    text += "Нажми «Начать», чтобы завести питомца 👇"
    kb = None
    with contextlib.suppress(Exception):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="▶️ Начать", callback_data="onb:start")]])
    try:
        await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)
        logger.info("celebrate: welcome DM sent to {}", user_id)
    except Exception as exc:
        logger.debug("celebrate: send to {} failed: {}", user_id, type(exc).__name__)

def schedule_celebration(bot, user_id: int, first_name: str = "") -> None:
    task = asyncio.get_running_loop().create_task(
        celebrate_subscription(bot, user_id, first_name))
    _celebrate_tasks.add(task)
    task.add_done_callback(_celebrate_tasks.discard)

def required_chats() -> list[tuple[str, str]]:
    st = get_settings()
    seen: set[str] = set()
    chats: list[tuple[str, str]] = []

    def _add(value: object, uname: str = "") -> None:
        key = str(value or "").strip()
        if key and key not in seen:
            seen.add(key)
            chats.append((key, uname))

    for cid in st.tracked_chat_ids:
        _add(str(cid))
    ch_num = str(st.channel_chat_id).strip() if st.channel_chat_id else ""
    ch_uname = (st.channel_username or "").lstrip("@").strip()
    if ch_num and ch_uname:
        seen.discard(ch_num)
        chats[:] = [c for c in chats if c[0] != ch_num]
        chats.append((ch_num, ch_uname))
    elif ch_num:
        _add(ch_num)
    elif ch_uname:
        _add(ch_uname)
    return chats

def watched_chat_ids() -> set[int]:
    ids: set[int] = set()
    for cid, _uname in required_chats():
        n = numeric_chat_id(cid)
        if n is not None:
            ids.add(n)
    return ids

def is_watched(chat_id: int | None) -> bool:
    if chat_id is None:
        return False
    ids = watched_chat_ids()
    return not ids or int(chat_id) in ids

def numeric_chat_id(target: str | int) -> int | None:
    s = str(target).lstrip("@")
    if s.startswith("-100"):
        s = s[4:]
    try:
        return int(s)
    except ValueError:
        return None

async def record_membership(user_id: int, chat_id: int | str | None = None, *,
                            first_name: str = "", username: str | None = None,
                            real_event: bool = True,
                            contacted: bool = False,
                            arrived: bool = True) -> None:
    cid = numeric_chat_id(chat_id) if chat_id is not None else None
    if cid is None and chat_id is not None:
        return
    try:
        from app.db.repositories import SubscriberRepository
        from app.db.session import session_factory
        async with session_factory() as session:
            await SubscriberRepository(session).record_membership(
                int(user_id), cid, first_name=first_name, username=username,
                real_event=real_event, contacted=contacted, arrived=arrived)
        if real_event and cid is not None:
            logger.info("access: registry {} <- chat {} ({})", user_id, cid,
                        "event" if contacted is False else "event+contact")
    except Exception as exc:
        logger.warning("access: register {}@{} failed: {}: {}",
                       user_id, chat_id, type(exc).__name__, str(exc)[:200])

async def known_subscriber_ids(user_ids: Iterable[int]) -> set[int]:
    ids = {int(u) for u in user_ids if u}
    if not ids:
        return set()
    try:
        from sqlalchemy import select

        from app.db.models import ChannelSubscriber
        from app.db.session import session_factory
        async with session_factory() as session:
            rows = (await session.execute(
                select(ChannelSubscriber.user_id, ChannelSubscriber.chats)
                .where(ChannelSubscriber.user_id.in_(ids)))).all()
        return {int(uid) for uid, chats in rows if list(chats or [])}
    except Exception as exc:
        logger.debug("access: registry read failed for {}: {}", user_ids, exc)
        return set()

async def remember_contact(user_id: int, *, first_name: str = "",
                           username: str | None = None) -> None:
    try:
        from app.db.repositories import SubscriberRepository
        from app.db.session import session_factory
        async with session_factory() as session:
            await SubscriberRepository(session).record_membership(
                int(user_id), None, first_name=first_name, username=username,
                real_event=False, contacted=True)
    except Exception as exc:
        logger.warning("access: remember_contact {} failed: {}: {}",
                       user_id, type(exc).__name__, str(exc)[:200])

async def refresh_registry(bot, user_id: int) -> bool:
    return await _refresh_registry_impl(bot, user_id)

async def registry_state(user_id: int) -> str:
    try:
        from app.db.models import ChannelSubscriber
        from app.db.session import session_factory
        async with session_factory() as session:
            row = await session.get(ChannelSubscriber, int(user_id))
        if row is None:
            return "NO ROW"
        return f"row exists, chats={list(row.chats or [])!r}"
    except Exception as exc:
        return f"read failed: {type(exc).__name__}: {str(exc)[:80]}"

async def api_status_for(bot, user_id: int) -> tuple[dict[str, str], bool]:
    statuses: dict[str, str] = {}
    errored = False
    for cid, uname in required_chats():
        target = f"@{uname}" if uname else cid
        try:
            member = await bot.get_chat_member(target, user_id)
            statuses[target] = str(getattr(member, "status", "") or "?")
        except Exception as exc:
            errored = True
            statuses[target] = f"error:{type(exc).__name__}"
    return statuses, errored

async def mtproto_status(target: str | int, user_id: int) -> tuple[str, str]:
    if isinstance(target, str) and target.startswith("@"):
        target = target[1:]
    try:
        from app.services.mtproto_client import get_chat_member_status
        status = await get_chat_member_status(target, user_id)
        if status is None:
            reason = ""
            with contextlib.suppress(Exception):
                from app.services.mtproto_client import last_scan_error
                reason = await last_scan_error()
            return ("silent", reason or "MTProto вернул None (нет клиента/прав/entity)")
        return ("ok", status)
    except Exception as exc:
        return ("error", f"{type(exc).__name__}: {str(exc)[:120]}")

async def scan_chat_participants(target: str) -> int:
    global LAST_SCAN_SEEN, LAST_SCAN_TOTAL
    from app.services.mtproto_sync import mtproto_configured

    if not mtproto_configured():
        return 0
    try:
        from app.services.mtproto_client import iter_all_participants
        members = await iter_all_participants(target)
    except Exception as exc:
        logger.debug("access: MTProto scan {} failed: {}", target, exc)
        return 0

    cid = numeric_chat_id(target)
    added = 0
    for m in members or []:
        try:
            uid = int(m["id"])
        except (KeyError, TypeError, ValueError):
            continue
        await record_membership(uid, cid, first_name=m.get("first_name") or "",
                                username=m.get("username"))
        added += 1

    LAST_SCAN_SEEN = len(members or [])
    total: int | None = None
    with contextlib.suppress(Exception):
        from app.services.mtproto_client import chat_participants_count
        total = await chat_participants_count(target)
    LAST_SCAN_TOTAL = total

    if added:
        logger.info("access: живой скан {} — {} участник(ов) в реестре",
                    target, added)
    if members and total and len(members) < max(2, total // 2):
        logger.error("access: ⚠️ скан {} собрал {} из ~{} участник(ов) — выборка "
                     "Telethon неполная; часть подписчиков осталась без записи "
                     "(проверьте показ списка участников в группе и права "
                     "MTProto-аккаунта)", target, len(members), total)
    return added

LAST_SCAN_SEEN: int | None = None
LAST_SCAN_TOTAL: int | None = None

_SCAN_COOLDOWN_SEC = 600
_scan_busy: set[str] = set()
_scan_last: dict[str, float] = {}

async def _refresh_registry_impl(bot, user_id: int) -> bool:
    import time

    ran = False
    for cid, uname in required_chats():
        target = uname or cid
        key = str(target)
        now = time.monotonic()
        if key in _scan_busy or _scan_last.get(key, 0.0) > now - _SCAN_COOLDOWN_SEC:
            continue
        _scan_busy.add(key)
        try:
            await scan_chat_participants(target)
            ran = True
        finally:
            _scan_busy.discard(key)
            _scan_last[key] = time.monotonic()
    return ran

async def last_scan_error_safe() -> str:
    with contextlib.suppress(Exception):
        from app.services.mtproto_client import last_scan_error
        return await last_scan_error() or ""
    return ""


