"""Глобальный доступ: только ЛС и только подписчики канала.

Правила бота (v1.5.3):
1. Взаимодействие с ботом — исключительно в личных сообщениях. В группах и
   каналах бот молчит: не отвечает на команды и кнопки, ничего не пишет
   (пассивный трекер активности остаётся — см. handlers/tracker.py).
2. Пользователь без подписки на канал (CHANNEL_USERNAME) не может
   взаимодействовать с ботом: вместо ответа — просьба подписаться.

Проверка подписки — Bot API getChatMember (бот обязан быть админом канала),
результат кэшируется в Redis/in-memory на SUBSCRIBE_CACHE_SEC, чтобы не
жечь лимит API на каждый тап. Если канал не настроен или Telegram вернул
ошибку — доступ разрешён (fail-open: иначе бот «умирает» при сбое API).
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject, User
from loguru import logger

from app.config import get_settings
from app.utils.redis import set_cooldown

SUBSCRIBE_CACHE_SEC = 300        # TTL кэша проверки подписки
_GRANTED_KEY = "sub_granted"     # ключ в context данных хендлера

# Подписчики со стажем: кулдаун-ключ живёт 5 минут, продлеваем его при каждом
# успешном запросе — повторная проверка API тогда вовсе не нужна.
_EXTEND_EVERY_SEC = 60

_pos_cache: dict[Any, float] = {}  # in-memory fallback: user_id/extend-ключ -> срок жизни


def subscribe_kb() -> "Any":
    """Клавиатура-заглушка для неподписанных: ссылка на канал + проверка."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    ch = get_settings().channel_username or ""
    rows = []
    if ch:
        rows.append([InlineKeyboardButton(text=f"📢 Подписаться: t.me/{ch}",
                                          url=f"https://t.me/{ch}")])
    rows.append([InlineKeyboardButton(text="✅ Я подписался — проверить",
                                      callback_data="gate:check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def is_channel_subscribed(bot, user_id: int) -> bool:
    """True — пользователь подписан на канал (или проверка недоступна)."""
    st = get_settings()
    if not st.channel_username:
        return True
    now = time.monotonic()
    exp = _pos_cache.get(user_id)
    if exp is not None and exp > now:
        return True
    fresh = await set_cooldown(f"sub:{user_id}", SUBSCRIBE_CACHE_SEC)
    if not fresh:
        # свежий положительный кэш уже есть — продлеваем не чаще раза в минуту
        if now >= _pos_cache.get(f"extend:{user_id}", 0.0):
            _pos_cache[f"extend:{user_id}"] = now + _EXTEND_EVERY_SEC
            await set_cooldown(f"sub:{user_id}", SUBSCRIBE_CACHE_SEC)
        return True
    try:
        member = await bot.get_chat_member(f"@{st.channel_username}", user_id)
    except TelegramAPIError as exc:
        logger.warning("subscription check failed for {} ({}): fail-open",
                       user_id, exc)
        return True
    if member.status in ("member", "administrator", "creator"):
        _pos_cache[user_id] = time.monotonic() + SUBSCRIBE_CACHE_SEC
        return True
    return False


def _target_user(event) -> User | None:
    if isinstance(event, CallbackQuery):
        return event.from_user
    if isinstance(event, Message):
        return event.from_user
    return None


class AccessGateMiddleware(BaseMiddleware):
    """Outer-middleware на все апдейты: приватные чаты + подписка на канал."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)

        chat = getattr(event, "chat", None)
        if chat is None:
            return await handler(event, data)

        # Правило 1: бот общается только в ЛС. В группах/каналах — полная тишина
        # (трекинг активности идёт через отдельный хендлер tracker.py напрямую).
        if chat.type != ChatType.PRIVATE:
            return None

        user = _target_user(event)
        if user is None or user.is_bot:
            return None

        # Правило 2: без подписки на канал взаимодействие запрещено.
        if not await is_channel_subscribed(data["bot"], user.id):
            text = ("🔒 Бот доступен только подписчикам канала.\n\n"
                    f"📢 Подпишись — и возвращайся, я жду!\n"
                    "После подписки нажми «Проверить» или отправь /start.")
            if isinstance(event, CallbackQuery):
                await event.answer("Сначала подпишись на канал 📢", show_alert=True)
                await event.message.edit_text(text, reply_markup=subscribe_kb())
            else:
                await event.answer(text, reply_markup=subscribe_kb())
            return None

        data[_GRANTED_KEY] = True
        return await handler(event, data)
