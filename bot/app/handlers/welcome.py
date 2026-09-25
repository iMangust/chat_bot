"""Хендлер приветствия новичков в группе (new_chat_members).

Сценарий:
1. Бот видит новых участников в отслеживаемой группе.
2. Пытается отправить каждому в ЛС персональное приветствие с кнопкой «Начать».
3. Если ЛС закрыты (TelegramForbiddenError) — упоминает новичка в группе
   с просьбой написать боту /start.

Реферал: награда пригласившему начисляется ОДНО место — в ActivityService
при первой засчитанной активности новичка (см. services/activity.py,
_credit_referral). Здесь мы только подхватываем deep-link-маркер из системного
сообщения «User joined Telegram by invite link» и сохраняем связку, если её
не удалось разобрать в /start бота.
"""
from __future__ import annotations

import html

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter,
)
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.config import get_settings
from app.db.repositories import UserRepository
from app.services.achievements import AchievementService
from app.handlers.start import WELCOME_DM
from app.keyboards.inline import welcome_start_button

router = Router(name="welcome")


def _is_tracked(chat_id: int) -> bool:
    ids = get_settings().tracked_chat_ids
    return not ids or chat_id in ids


def parse_invite_marker(text: str | None) -> int | None:
    """Достаёт tg_id пригласившего из текста «...invite_<id>...» (или None)."""
    if not text or "invite_" not in text:
        return None
    try:
        return int(text.split("invite_", 1)[1].split()[0])
    except (ValueError, IndexError):
        return None


@router.message(F.new_chat_members)
async def on_new_members(message: Message, bot: Bot, session: AsyncSession) -> None:
    if message.chat.type not in ("group", "supergroup") or not _is_tracked(message.chat.id):
        return

    users = UserRepository(session)
    greeters: list[str] = []
    inviter = parse_invite_marker(message.text)

    for member in message.new_chat_members:
        if member.is_bot:
            continue
        # регистрируем «заготовку» — onboarded=False до нажатия «Начать»
        await users.get_or_create(member.id, member.first_name, member.username)
        # реферал: если новичок пришёл по deep-link `start=invite_<tg_id>` —
        # сохраняем связку; сама награда выдаётся после первой активности
        if inviter and inviter != member.id:
            if await users.set_referrer(member.id, inviter):
                logger.info("referral link stored: inviter={} new={}", inviter, member.id)
        try:
            await bot.send_message(
                member.id,
                WELCOME_DM.format(name=html.escape(member.first_name or "друг"),
                                  channel_line=_channel_line_safe()),
                reply_markup=welcome_start_button(),
            )
            greeters.append(f"<a href='tg://user?id={member.id}'>{html.escape(member.first_name or 'друг')}</a>")
            logger.info("welcome DM sent to {}", member.id)
        except TelegramForbiddenError:
            # ЛС закрыты — попросим написать сами (упоминание соберём ниже)
            greeters.append(f"@{member.username or member.first_name} (ЛС закрыты — напиши мне /start!)")
        except TelegramRetryAfter as e:
            logger.warning("rate limited while welcoming {}: sleep {}", member.id, e.retry_after)
        except TelegramAPIError as e:
            logger.error("failed to welcome {}: {}", member.id, e)

    if greeters and message.chat.type == "supergroup":
        names = ", ".join(greeters)
        await message.answer(
            f"🎉 Добро пожаловать, {names}!\n"
            f"Я бот-компаньон: за активность тут дают XP, монеты и достижения, "
            f"а ещё можно завести питомца. Проверь личные сообщения от меня 🐾"
        )


def _channel_line_safe() -> str:
    ch = get_settings().channel_username
    return f"📢 Наш канал: t.me/{ch}\n" if ch else ""
