"""Глобальный доступ: только ЛС и только подписчики отслеживаемых чатов.

Правила бота (v1.5.6):
1. Взаимодействие с ботом — исключительно в личных сообщениях. В группах и
   каналах бот молчит: не отвечает на команды и кнопки, ничего не пишет
   (пассивный трекер активности остаётся — см. handlers/tracker.py).
2. Пользователь без подписки хотя бы на ОДИН из TRACKED_CHAT_IDS (а если
   список пуст — на канал CHANNEL_USERNAME / CHANNEL_CHAT_ID) не может
   взаимодействовать с ботом: вместо ответа — просьба подписаться.
   Раньше проверялся только CHANNEL_USERNAME; при его отсутствии гейт
   жил в режиме fail-open и правило подписки не работало вовсе.

Проверка подписки — Bot API getChatMember (бот обязан быть админом чата),
результат кэшируется in-memory на SUBSCRIBE_CACHE_SEC, чтобы не жечь лимит
API на каждый тап. При сбое Telegram действует fail-open (бот не должен
«мирать» из-за недоступности API); если ни один чат не настроен — доступ
разрешён (dev-режим), но админ получает разовое предупреждение. Особый
случай — TelegramForbiddenError «bot must be an administrator»: это
настройка окружения, а не сбой; fail-open тоже применяется, но событие
логируется как ERROR и подсвечивается админу (ADMIN_IDS), чтобы проблема
не осталась незамеченной.
"""
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

from app.config import get_settings

SUBSCRIBE_CACHE_SEC = 300        # TTL положительного кэша проверки подписки
_NEG_TTL_SEC = 15                # короткий кэш «не подписан», чтобы не долбить API
_GRANTED_KEY = "sub_granted"     # ключ в context данных хендлера

_pos_cache: dict[Any, float] = {}  # user_id -> monotonic-срок жизни «подписан»


class _SyntheticPrivateChat:
    """Заглушка чата: приватный тип для ЛС-колбэков без message.chat."""
    type = ChatType.PRIVATE


_neg_cache: dict[Any, float] = {}  # user_id -> monotonic-срок жизни «не подписан»
_warned_no_admin: set[str] = set()  # каналы, по которым уже били в лог/админу
_warned_no_gating: set[str] = set()  # «гейт выключен» (чат не настроен) — разово


def required_chats() -> list[tuple[str, str]]:
    """Чаты, подписка хотя бы на ОДИН из которых обязательна: [(id, username)].

    Источник — TRACKED_CHAT_IDS (см. config): взаимодействие разрешено только
    подписчикам одного из отслеживаемых канала/группы. Если список пуст,
    используем CHANNEL_CHAT_ID / CHANNEL_USERNAME (одиночный канал).
    """
    st = get_settings()
    chats: list[tuple[str, str]] = [(str(cid), "") for cid in st.tracked_chat_ids]
    if not chats and (st.channel_chat_id or st.channel_username):
        chats.append((str(st.channel_chat_id or ""), st.channel_username or ""))
    return chats


def subscribe_kb() -> "Any":
    """Клавиатура-заглушка для неподписанных: ссылка на канал + проверка."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    st = get_settings()
    rows = []
    ch = st.channel_username or ""
    if not ch:
        # юзернейма канала нет — ведём на первый отслеживаемый чат по ID
        for cid, uname in required_chats():
            if uname:
                ch = uname
                break
            if cid.startswith("-100"):
                # t.me/+<внутренний id> — рабочая ссылка-приглашение для
                # приватных каналов/групп без юзернейма
                ch = f"+{cid[4:]}"
                break
    if ch:
        rows.append([InlineKeyboardButton(text=f"📢 Подписаться: t.me/{ch}",
                                          url=f"https://t.me/{ch}")])
    rows.append([InlineKeyboardButton(text="✅ Я подписался — проверить",
                                      callback_data="gate:check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reset_subscribe_cache(user_id: int | None = None) -> None:
    """Сбрасывает кэш проверки подписки (по пользователю или весь целиком)."""
    if user_id is None:
        _pos_cache.clear()
        _neg_cache.clear()
    else:
        _pos_cache.pop(user_id, None)
        _neg_cache.pop(user_id, None)


async def _notify_admin(bot, text_key: str, uid: str, text: str) -> None:
    """Разово предупреждает админа (ADMIN_IDS) о проблеме конфигурации гейта."""
    if uid in _warned_no_admin or uid in _warned_no_gating:
        return
    if text_key == "no_admin":
        _warned_no_admin.add(uid)
    else:
        _warned_no_gating.add(uid)
    ids = get_settings().admin_ids
    if not ids:
        return
    with contextlib.suppress(Exception):
        await bot.send_message(ids[0], text)


async def is_channel_subscribed(bot, user_id: int) -> bool:
    """True — пользователь подписан хотя бы на ОДИН обязательный чат
    (TRACKED_CHAT_IDS; при пустом списке — CHANNEL_USERNAME/CHANNEL_CHAT_ID),
    либо проверка недоступна (fail-open).

    Положительный результат кэшируется на SUBSCRIBE_CACHE_SEC, отрицательный —
    на _NEG_TTL_SEC (короткий, чтобы «Я подписался» срабатывало почти сразу).
    Любая ошибка API => fail-open: бот обязан оставаться отзывчивым даже при
    недоступном канале/сбое Telegram — молчание в ЛС недопустимо.
    """
    chats = required_chats()
    if not chats:
        # Ни один чат не настроен — проверять подписку негде: доступ открыт,
        # но админ получает разовое предупреждение (правило 2 не работает).
        logger.error("subscription gate disabled: TRACKED_CHAT_IDS/CHANNEL_* are empty — "
                     "anyone can use the bot")
        asyncio.ensure_future(_notify_admin(
            bot, "no_gating", "no-gating",
            "⚠️ Проверка подписки отключена: не заданы TRACKED_CHAT_IDS и "
            "CHANNEL_USERNAME/CHANNEL_CHAT_ID. Любой пользователь может "
            "взаимодействовать с ботом — настройте обязательные чаты."))
        return True
    now = time.monotonic()
    pos = _pos_cache.get(user_id)
    if pos is not None and pos > now:
        return True
    neg = _neg_cache.get(user_id)
    if neg is not None and neg > now:
        return False
    api_error = False
    for cid, uname in chats:
        target = f"@{uname}" if uname else cid
        try:
            member = await bot.get_chat_member(target, user_id)
        except TelegramForbiddenError as exc:
            # Бот не админ чата / неверный ID: это настройка, а не сбой
            # на секунду. Пробуем следующий чат; fail-open + предупреждение.
            api_error = True
            logger.error("cannot check subscription for {}: {} — fail-open",
                         target, exc)
            asyncio.ensure_future(_notify_admin(
                bot, "no_admin", target,
                f"⚠️ Не могу проверять доступ ({target}): бот должен быть "
                "администратором канала с правом «Добавлять администраторов» "
                "(Add Admins). Пока доступ работает в режиме разрешения (fail-open)."))
            continue
        except TelegramAPIError as exc:
            api_error = True
            logger.warning("subscription check failed for {} ({}): skip chat",
                           target, exc)
            continue
        if member.status in ("member", "administrator", "creator"):
            _pos_cache[user_id] = time.monotonic() + SUBSCRIBE_CACHE_SEC
            _neg_cache.pop(user_id, None)
            return True
    if api_error:
        # ни в один чат проверить не удалось — не глушим бота из-за сбоя
        return True
    _neg_cache[user_id] = time.monotonic() + _NEG_TTL_SEC
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
        # иначе кнопка из группы просочилась бы к хендлерам. Если сообщение
        # отправлено в канал (forward) и чата нет вовсе, берём чат автора
        # колбэка; если и там пусто — не блокируем апдейт молча.
        chat = getattr(event, "chat", None) or getattr(
            getattr(event, "message", None), "chat", None)
        if chat is None and isinstance(event, CallbackQuery):
            chat = getattr(event.from_user, "_private_chat", None)                 or _SyntheticPrivateChat()
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

        try:
            subscribed = await is_channel_subscribed(data["bot"], user.id)
        except Exception as exc:  # noqa: BLE001 — любая ошибка проверки => доступ открыт
            logger.warning("subscription gate crashed for {}: {} — allow", user.id, exc)
            subscribed = True

        # Правило 2: без подписки на канал взаимодействие запрещено. Команды
        # /start и /help работают всегда — иначе неподписанный не сможет
        # начать (сценарий «только что установил бота»). Остальные кнопки и
        # текст — только после подтверждения подписки.
        exempt = isinstance(event, Message) and _is_entry_command(event)
        if not exempt and not subscribed:
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
