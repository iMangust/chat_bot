from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import CallbackQuery
from loguru import logger

error_router = Router(name="errors")

@error_router.errors()
async def on_error(event: Any, exception: Exception | None = None, **kwargs: Any) -> Any:
    exc = exception
    if exc is None and hasattr(event, "exception"):
        exc = event.exception
    upd = getattr(event, "update", event)
    if isinstance(upd, CallbackQuery):
        event = upd
    logger.opt(exception=exc).error("unhandled error while processing update: {}",
                                    type(exc).__name__ if exc else "?")
    if isinstance(event, CallbackQuery):
        try:
            await event.answer("Упс, что-то пошло не так 😅 Попробуй ещё раз.",
                               show_alert=True)
        except TelegramAPIError:
            pass
    return True

class ErrorNotifyMiddleware(BaseMiddleware):

    async def __call__(
        self,
        handler: Callable[[CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except TelegramForbiddenError:
            return None
        except TelegramAPIError as exc:
            if "query is too old" in str(exc) or "INVALID_QUERY" in str(exc).upper():
                logger.debug("callback query expired before answer: {}", event.data)
                return None
            logger.warning("telegram api error in {}: {}", event.data, exc)
            try:
                await event.answer("Не получилось 😅 Попробуй ещё раз.", show_alert=True)
            except TelegramAPIError:
                pass
            return None
