from __future__ import annotations

import contextlib

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, ChatMemberUpdated, Message,
                           TelegramObject)
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from collections.abc import Awaitable, Callable
from typing import Any

from app.config import get_settings
from app.services import access as access_service

router = Router(name="access")

class AccessEventsMiddleware(BaseMiddleware):

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        with contextlib.suppress(Exception):
            if isinstance(event, ChatMemberUpdated):
                await handle_chat_member(event, data["bot"])
            elif isinstance(event, Message):
                if event.new_chat_members:
                    await handle_new_chat_members(event)
                elif _is_private_start(event):
                    u = event.from_user
                    await access_service.remember_contact(
                        u.id, first_name=u.first_name or "",
                        username=u.username)
        return await handler(event, data)

def _is_private_start(message: Message) -> bool:
    if message.chat.type != ChatType.PRIVATE:
        return False
    text = (message.text or "").split("@", 1)[0].strip().lower()
    return text in {"/start", "/help"} and message.from_user is not None

def watched_chat_ids() -> set[int]:
    return access_service.watched_chat_ids()

async def register_member(user_id: int, chat_id: int | str | None = None, *,
                          first_name: str = "", username: str | None = None,
                          real_event: bool = True, contacted: bool = False) -> None:
    await access_service.record_membership(
        user_id, chat_id, first_name=first_name, username=username,
        real_event=real_event, contacted=contacted)

async def remember_contact(user_id: int, *, first_name: str = "",
                           username: str | None = None) -> None:
    await access_service.remember_contact(user_id, first_name=first_name,
                                          username=username)

async def ensure_registry_fresh(bot: Bot, user_id: int) -> bool:
    return await access_service.refresh_registry(bot, user_id)

def last_scan_stats() -> tuple[int | None, int | None]:
    return (access_service.LAST_SCAN_SEEN, access_service.LAST_SCAN_TOTAL)

async def scan_channel_participants(target: str) -> int:
    return await access_service.scan_chat_participants(target)

async def handle_chat_member(update: ChatMemberUpdated, bot: Bot) -> None:
    if not access_service.is_watched(update.chat.id):
        return
    member = update.new_chat_member
    user = getattr(member, "user", None)
    if user is None or user.is_bot or user.id == bot.id:
        return
    status = str(getattr(member, "status", "") or "")
    arrived = status in (ChatMemberStatus.MEMBER.value,
                         ChatMemberStatus.ADMINISTRATOR.value,
                         ChatMemberStatus.CREATOR.value)
    await register_member(user.id, update.chat.id,
                          first_name=user.first_name or "", username=user.username,
                          real_event=True)
    old_status = str(getattr(getattr(update, "old_chat_member", None), "status", "") or "")
    logger.info("access: chat_member {} in {} : {} -> {}", user.id, update.chat.id,
                old_status or "?", status + ("" if arrived else " (не член)"))

@router.chat_member()
async def _chat_member_handler(update: ChatMemberUpdated, bot: Bot) -> None:
    await handle_chat_member(update, bot)

@router.message(F.chat.type.in_({"group", "supergroup", "channel"}) & F.new_chat_members)
async def handle_new_chat_members(message: Message) -> None:
    if not access_service.is_watched(message.chat.id):
        return
    for member in message.new_chat_members or []:
        if member.is_bot:
            continue
        await register_member(member.id, message.chat.id,
                              first_name=member.first_name or "",
                              username=member.username, real_event=True)

@router.message(Command("subscribers"))
async def cmd_subscribers(message: Message, session: AsyncSession) -> None:
    st = get_settings()
    if message.chat.type != ChatType.PRIVATE:
        return
    if message.from_user is None or message.from_user.id not in st.admin_ids:
        return
    from app.db.repositories import SubscriberRepository

    repo = SubscriberRepository(session)
    stats = await repo.registry_stats()
    chats = access_service.required_chats()
    lines = [
        "👥 <b>Реестр доступа</b>",
        f"• человек в реестре: {stats['total']}",
        f"• из них писали боту сами: {stats['contacted']}",
        f"• обязательных чатов: {len(chats)}",
    ]
    for cid, uname in chats:
        target = uname or cid
        try:
            members = await repo.member_ids_by_chat(
                access_service.numeric_chat_id(target) or int(target))
            count = len(members)
        except Exception as exc:
            count = -1
            logger.debug("registry per-chat count failed for {}: {}", target, exc)
        lines.append(f"   • @{target}: {count if count >= 0 else '?'} записей")
    if not chats:
        lines.append("⚠️ Чаты не настроены (TRACKED_CHAT_IDS / CHANNEL_* пустые) — "
                     "гейт выключен, доступ открыт всем.")
    lines.append("")
    lines.append("Проверка конкретного человека: /accessdebug <user_id>")
    await message.answer("\n".join(lines))

@router.callback_query(F.data == "gate:check")
async def cb_gate_check(cb: CallbackQuery, bot: Bot) -> None:
    from app.middlewares.gate import is_channel_subscribed, reset_subscribe_cache

    reset_subscribe_cache(cb.from_user.id)
    allowed = await is_channel_subscribed(bot, cb.from_user.id)
    if allowed:
        await cb.message.edit_text(
            "✅ Подписка подтверждена!\n\nНажми «Начать», чтобы попасть в меню.",
            reply_markup=_start_kb())
        await cb.answer("Готово 🎉")
        return

    diag: list[str] = []
    statuses, _failed = await access_service.api_status_for(bot, cb.from_user.id)
    diag.append("Bot API: " + "; ".join(f"{k}={v}" for k, v in statuses.items()))
    diag.append(f"Реестр: {await access_service.registry_state(cb.from_user.id)}")
    mt = []
    for cid, uname in access_service.required_chats():
        kind, res = await access_service.mtproto_status(uname or cid, cb.from_user.id)
        mt.append(f"@{uname or cid}: {kind} {str(res)[:60]}")
    diag.append("MTProto: " + ("; ".join(mt) if mt else "нет чатов"))
    scan_err = await access_service.last_scan_error_safe()
    if scan_err:
        diag.append(f"Скан: {scan_err[:120]}")
    logger.info("gate:check DENY {}: {}", cb.from_user.id, " | ".join(diag))
    await cb.answer(
        "Подписка не найдена 😔\nЕсли ты точно в канале или группе — напиши "
        "/start ещё раз через пару минут: бот перепроверит по всем источникам.",
        show_alert=True)
    with contextlib.suppress(TelegramForbiddenError, Exception):
        await notify_admins(bot,
                            f"🧾 Проверка доступа не пройдена: <code>{cb.from_user.id}</code>\n"
                            + "\n".join(access_service.__dict__.get("_esc", lambda s: s)(d)
                                        for d in diag))

def _start_kb():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Начать", callback_data="onb:start")]])

async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in get_settings().admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:
            logger.debug("notify admin {} failed: {}", admin_id, str(exc)[:80])
