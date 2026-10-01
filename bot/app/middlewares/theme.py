"""Middleware темы оформления: ставит активную тему в контекст обновления.

Тема хранится в ``User.settings_extra["theme"]`` (JSON-колонка users уже
есть — отдельная миграция не нужна). Для каждого апдейта подгружаем юзера
(без коммита: только чтение) и кладём ключ темы в contextvar, который
читают сборщики клавиатур, i18n-строки и эффекты (app/themes.py).

Если юзера в БД ещё нет (первый /start до get_or_create) или колонка
пустая — работает стандартная тема.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app import themes


class ThemeMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[TelegramObject, dict],
                                                Awaitable[Any]],
                       event: TelegramObject,
                       data: dict) -> Any:
        user = getattr(event, "from_user", None) or getattr(event, "chat", None)
        tg_id = getattr(user, "id", None)
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
        return await handler(event, data)
