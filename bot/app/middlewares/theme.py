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
            session = data.get("session")
            theme_key: str | None = None
            if session is not None:
                try:
                    from app.db.models import User

                    db_user = await session.get(User, int(tg_id))
                    extra = (db_user.settings_extra or {}) if db_user else {}
                    theme_key = extra.get("theme")
                except Exception:
                    theme_key = None
            if theme_key is None:
                # юзера ещё нет в БД (или колонка пустая): не затираем тему,
                # которую только что установил обработчик выбора темы
                # («set:theme:*» пишет её сразу в контекст и в БД).
                theme_key = themes.current_theme_key()
            themes.set_theme(theme_key)
        return await handler(event, data)
