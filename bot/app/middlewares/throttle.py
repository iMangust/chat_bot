"""Мидлвар throttling для callback-кнопок: защита от двойных тапов и спама.

Логика: на связку (user_id, callback_data) в Redis ставится кулдаун 1.5 сек.
Повторные нажатия молча игнорируются (answer с «Подожди…»).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject

from app.utils.redis import set_cooldown

THROTTLE_SEC = 1.5


class ThrottleMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        key = f"cb:{event.from_user.id}:{event.data}"
        ok = await set_cooldown(key, THROTTLE_SEC)
        if not ok:
            await event.answer("⏳ Подожди секунду…", show_alert=False)
            return None
        return await handler(event, data)
