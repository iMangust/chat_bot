"""Middleware темы оформления: ставит активную тему в контекст обновления.

Тема хранится в ``User.settings_extra["theme"]`` (JSON-колонка users уже
есть — отдельная миграция не нужна). Для каждого апдейта подгружаем юзера
(без коммита: только чтение) и кладём ключ темы в contextvar, который
читают сборщики клавиатур, i18n-строки и эффекты (app/themes.py).

Если юзера в БД ещё нет (первый /start до get_or_create) или колонка
пустая — работает стандартная тема.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app import themes


def _event_tg_id(event: TelegramObject) -> int | None:
    """tg_id пользователя, который УВИДИТ результат этого апдейта.

    Для callback_query это cb.from_user; но у Message/EditedMessage поле
    ``from_user`` — автор сообщения, а не адресат. В ГРУППАХ это привело бы
    к тому, что ответ бота (например, на «+5 XP» за сообщение Васи)
    собирался в теме автора, а не того, кто его прочитает.

    Ключевое различие:
      • private-чат: собеседник один — тема автора сообщения и есть тема
        адресата (в ЛС пользователь пишет сам себе);
      • группа/супергруппа: адресат — ВСЕ участники, индивидуальную тему
        применить нельзя, поэтому всегда стандартная. Раньше для групп
        брался автор (или автор reply) — из-за этого процессный кэш тем
        постоянно перезаписывался под разных людей, и главный пользователь
        видел свой выбор «только в настройках»: стоило кому-то написать в
        чат, как контекст следующих экранов становился стандартным.
    """
    if isinstance(event, CallbackQuery):
        return getattr(getattr(event, "from_user", None), "id", None)
    msg = event if isinstance(event, Message) else (
        getattr(event, "message", None) or getattr(event, "edited_message", None))
    if not isinstance(msg, Message):
        return getattr(getattr(event, "from_user", None), "id", None)
    chat_type = getattr(getattr(msg, "chat", None), "type", "")
    # Группы/каналы: ответ бота увидят все участники — индивидуальная тема
    # неприменима, всегда стандартная (см. docstring).
    if chat_type != "private":
        return None
    return getattr(getattr(msg, "from_user", None), "id", None)


class ThemeMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[TelegramObject, dict],
                                                Awaitable[Any]],
                       event: TelegramObject,
                       data: dict) -> Any:
        tg_id = _event_tg_id(event)
        if tg_id is not None:
            # Читаем тему из БД сами — НЕ полагаемся на data["session"]:
            # aiogram отдаёт kwargs хендлеру только из словаря того уровня
            # middleware, где был вызван handler, а сессию кладёт DbMiddleware
            # (более внешний слой). Без этого шага тема молча оставалась бы
            # стандартной для всех экранов, кроме настроек (там она
            # подтягивается из БД явно).
            # load_theme_key кэширует значение на процесс (см. app/themes.py),
            # поэтому на апдейт приходится максимум один короткий SELECT.
            theme_key = await themes.load_theme_key(int(tg_id))
            if theme_key is None:
                # юзера ещё нет в БД (первый /start до get_or_create) или
                # колонка пустая: не затираем тему, которую только что
                # установил обработчик «set:theme:*» (он пишет её сразу
                # в контекст и БД + инвалидирует кэш).
                theme_key = themes.current_theme_key()
            themes.set_theme(theme_key)
            # Запоминаем владельца темы — это нужно error-handler'ам aiogram:
            # они работают в НОВОМ контексте asyncio.Task, где contextvar
            # не наследуется, и без этой метки фолбэк «unhandled menu
            # callback» перерисовывал экраны стандартной темой.
            themes.remember_theme_owner(int(tg_id))
        return await handler(event, data)


class ThemeErrorMiddleware(BaseMiddleware):
    """Восстанавливает тему пользователя в контексте обработчика ошибок.

    aiogram вызывает error-handler'ы в отдельной задаче — CURRENT_THEME там
    всегда default(standard). Читаем tg_id из события и возвращаем выбранную
    пользователем тему из процессного кэша, чтобы любые ответные экраны
    (в т.ч. перерисовка меню фолбэком) были в стиле пользователя.
    """

    async def __call__(self, handler: Callable[[Any, dict], Awaitable[Any]],
                       exception: Any, data: dict) -> Any:
        event = data.get("event_update")
        if event is not None:
            tg_id = _event_tg_id(event)
            if tg_id is not None:
                themes.ensure_theme_for(int(tg_id))
                themes.remember_theme_owner(int(tg_id))
        return await handler(exception, data)


class ThemeGuardMiddleware(BaseMiddleware):
    """Гарант темы непосредственно перед каждым хендлером (inner-middleware).

    История багов: тема «применялась только к настройкам». Причина — outer
    ThemeMiddleware ставит contextvar в задаче апдейта, но между ним и
    конкретным хендлером стоят другие слои (AccessGate, Throttle, FSM),
    некоторые из которых могут переключить контекст или вернуть standard
    для неизвестного пользователя. Плюс у Message-хендлеров адресат и автор
    различаются. Поэтому прямо перед исполнением хендлера мы ещё раз
    убеждаемся, что в контексте стоит тема именно того пользователя, чей
    это апдейт (по процессному кэшу — без обращения к БД).
    """

    async def __call__(self, handler: Callable[[Any, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        tg_id = _event_tg_id(event)
        if tg_id is None:
            # из data может быть доступен пользователь, смонтированный
            # более внешними middleware (например, DbMiddleware)
            user = getattr(event, "from_user", None)
            tg_id = getattr(user, "id", None)
        if tg_id is not None:
            themes.ensure_theme_for(int(tg_id))
            themes.remember_theme_owner(int(tg_id))
            # ЖЁСТКАЯ ГАРАНТИЯ. Если outer-слой не поставил тему в контекст
            # (аппдейт исполняется в новом asyncio-контексте, где contextvar
            # = default standard), ensure_theme_for восстанавливает её из
            # кэша. Но если контекст всё ещё расходится с тем, что реально
            # лежит в БД для этого пользователя, читаем БД один раз и
            # ставим тему принудительно. Кэш при этом прогревается, так что
            # повторных чтений на следующие апдейты не будет.
            cached = themes._THEME_CACHE.get(int(tg_id))
            current = themes.current_theme_key()
            if cached is None or cached != current:
                db_key = await themes.load_theme_key(int(tg_id))
                themes.set_theme(db_key if db_key is not None else current)
                themes.remember_theme_owner(int(tg_id))
        elif themes.active_theme_owner() is not None:
            # событие без явного адресата (например, системное) — оставляем
            # тему последнего активного пользователя, а не сбрасываем на standard
            themes.ensure_theme_for(themes.active_theme_owner())
        return await handler(event, data)
