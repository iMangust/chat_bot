"""Хендлер приветствия новичков в группе (new_chat_members).

Сценарий:
1. Бот видит новых участников в отслеживаемой группе.
2. Пытается отправить каждому в ЛС персональное приветствие с кнопкой «Начать».
3. Если ЛС закрыты (TelegramForbiddenError) — упоминает новичка в группе
   с просьбой написать боту /start.
"""
from __future__ import annotations

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


@router.message(F.new_chat_members)
async def on_new_members(message: Message, bot: Bot, session: AsyncSession) -> None:
    if message.chat.type not in ("group", "supergroup") or not _is_tracked(message.chat.id):
        return

    users = UserRepository(session)
    greeters: list[str] = []

    for member in message.new_chat_members:
        if member.is_bot:
            continue
        # регистрируем «заготовку» — onboarded=False до нажатия «Начать»
        await users.get_or_create(member.id, member.first_name, member.username)
        # реферал: если новичок пришёл по deep-link `start=invite_<tg_id>`
        if message.text and message.text.startswith("/start invite_"):
            try:
                inviter = int(message.text.split("invite_", 1)[1].split()[0])
            except (ValueError, IndexError):
                inviter = None
            if inviter and inviter != member.id:
                gained = await users.bump_stat(inviter, "invites", 1)
                await users.add_xp_coins(inviter, xp=30, coins=get_settings().invite_reward_coins)
                await AchievementService(session).check(inviter, {"invites": gained})
                logger.info("referral credited: inviter={} new={}", inviter, member.id)
        try:
            await bot.send_message(
                member.id,
                WELCOME_DM.format(name=member.first_name),
                reply_markup=welcome_start_button(),
            )
            greeters.append(f"<a href='tg://user?id={member.id}'>{member.first_name}</a>")
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
