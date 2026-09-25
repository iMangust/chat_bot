"""Глобальный error-handler: ни один упавший апдейт не должен «съедать» UX.

Без него любая TelegramBadRequest / необработанная исключение внутри хендлера
приводит к тому, что callback «висит» (часы на кнопке), а пользователь теряет
возможность вернуться назад. Здесь мы:
  • логируем ошибку;
  • для callback'ов отвечаем пользователю понятным тостом и НЕ даём апдейту
    упасть (возвращаем True — aiogram считает событие обработанным).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import CallbackQuery, TelegramObject
from loguru import logger

error_router = Router(name="errors")


@error_router.errors()
async def on_error(event: TelegramObject, exception: Exception, **kwargs: Any) -> Any:
    logger.opt(exception=exception).error("unhandled error while processing update: {}",
                                          type(exception).__name__)
    # Если это callback — обязательно «погасим» часы у пользователя,
    # иначе кнопка крутится вечно и экран кажется сломанным.
    if isinstance(event, CallbackQuery):
        try:
            await event.answer("Упс, что-то пошло не так 😅 Попробуй ещё раз.",
                               show_alert=True)
        except TelegramAPIError:
            pass
    return True  # считаем ошибку обработанной — диспетчер не падает


class ErrorNotifyMiddleware(BaseMiddleware):
    """Дублирует страховку на уровне callback'ов (на случай, если ошибка
    возникла ДО попадания в хендлер, например в мидлваре throttle/FSM)."""

    async def __call__(
        self,
        handler: Callable[[CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except TelegramForbiddenError:
            # юзер заблокировал бота — это нормально, не логируем как ошибку
            return None
        except TelegramAPIError as exc:
            logger.warning("telegram api error in {}: {}", event.data, exc)
            try:
                await event.answer("Не получилось 😅 Попробуй ещё раз.", show_alert=True)
            except TelegramAPIError:
                pass
            return None
