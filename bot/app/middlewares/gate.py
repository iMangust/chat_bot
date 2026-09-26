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

import contextlib

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject, User
from loguru import logger

from app.config import get_settings

SUBSCRIBE_CACHE_SEC = 300        # TTL положительного кэша проверки подписки
_GRANTED_KEY = "sub_granted"     # ключ в context данных хендлера

_pos_cache: dict[Any, float] = {}  # user_id -> monotonic-срок жизни «подписан»


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
    """True — пользователь подписан на канал (или проверка недоступна).

    Кэш только положительный и короткий (SUBSCRIBE_CACHE_SEC): отписка
    обнаруживается максимум через столько же секунд. Отрицательный результат
    не кэшируется вовсе — чтобы «Я подписался» / повторный /start сработали
    сразу, без ожидания протухания кеша.
    """
    st = get_settings()
    if not st.channel_username:
        return True
    now = time.monotonic()
    exp = _pos_cache.get(user_id)
    if exp is not None and exp > now:
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


_ENTRY_COMMANDS = {"start", "help"}


def _is_entry_command(message: Message) -> bool:
    """Команда входа (/start, /help) — работает и для неподписанных."""
    text = message.text or ""
    if not text.startswith("/"):
        return False
    cmd = text[1:].split()[0].split("@")[0].lower()
    return cmd in _ENTRY_COMMANDS


def _is_serviceable_group_message(event) -> bool:
    """Групповое сообщение, которое ведут служебные (безмолвные) хендлеры.

    Пропускаем к диспетчеру только служебные события: приход/уход участника
    (учёт подписчиков + приветствие уходит им в ЛС) и обычные текстовые/
    медиа-сообщения (пассивный трекер активности ничего не пишет в чат).
    Команды (/start и т.п.) в группах глотаются целиком — бот на них молчит.
    """
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
    """Outer-middleware на все апдейты: приватные чаты + подписка на канал."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)

        # у callback-запросов чат лежит на исходном сообщении — проверяем его,
        # иначе кнопка из группы просочилась бы к хендлерам
        chat = getattr(event, "chat", None) or getattr(
            getattr(event, "message", None), "chat", None)
        if chat is None:
            return await handler(event, data)

        if chat.type != ChatType.PRIVATE:
            # Правило 1: бот отвечает строго в ЛС, в группах/каналах молчит.
            # В диспетчер пропускаются только служебные хендлеры, которые
            # НИЧЕГО не пишут в чат: пассивный трекер активности и учёт
            # новых участников (приветствие уходит им в ЛС). Все текстовые
            # сообщения, команды и callback'и в группах глотаются здесь —
            # так «/start» или кнопка в группе не вызывают никакого ответа.
            if not isinstance(event, Message):
                with contextlib.suppress(Exception):
                    await event.answer()
                return None
            if _is_serviceable_group_message(event):
                return await handler(event, data)
            return None

        user = _target_user(event)
        if user is None:
            return await handler(event, data)   # служебные ЛС-апдейты без автора
        if user.is_bot:
            return None                          # боты не взаимодействуют с ботом

        # Правило 2: без подписки на канал взаимодействие запрещено. Команды
        # /start и /help работают всегда — иначе неподписанный не сможет
        # начать (сценарий «только что установил бота»). Остальные кнопки и
        # текст — только после подтверждения подписки.
        exempt = isinstance(event, Message) and _is_entry_command(event)
        if not exempt and not await is_channel_subscribed(data["bot"], user.id):
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
