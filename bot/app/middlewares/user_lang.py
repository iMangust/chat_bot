"""Мидлвар языка пользователя (i18n).

Ставит contextvars-язык из ``users.lang`` ДО выполнения хендлера, чтобы
``app.i18n.t(...)`` внутри любых слоёв (хендлеры, сервисы) видел правильный
язык. Дешёвый SELECT по PK; при отсутствии юзера/БД — дефолт RU.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.i18n import DEFAULT_LANG, set_current_lang


class UserLanguageMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        lang = DEFAULT_LANG
        try:
            user = getattr(event, "from_user", None)
            session = data.get("session")
            if user is not None and session is not None:
                from app.db.models import User
                db_user = await session.get(User, user.id)
                if db_user is not None and db_user.lang:
                    lang = db_user.lang
        except Exception:  # noqa: BLE001 — язык не критичен, деградим молча
            pass
        set_current_lang(lang)
        return await handler(event, data)
