"""Middleware стека навигации: запоминаем, ОТКУДА пользователь пришёл на экран.

Работает для всех callback-роутеров сразу (регистрируется в main.py до
include_routers): перед обработкой коллбэка кладём его data в стек чата
(app/utils/nav.py), а после — обновляем «зеркало» вершины стека в памяти
(mem_stack), по которому синхронные сборщики клавиатур рисуют кнопку
«⬅️ Назад». Так «Назад» из мерча ведёт в категорию мерча, из категории —
в экран мерча, а не в меню питомца.

Исключения:
  • кнопки «🏠 Меню» (menu:main/menu:home) — при выходе в главное меню стек
    сбрасывается целиком;
  • клики внутри экрана (пагинация, noop, действия) фильтруются в nav.remember.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject

from app.utils import nav


class NavStackMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[TelegramObject, dict],
                                                Awaitable[Any]],
                       event: TelegramObject,
                       data: dict) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)

        chat_id = event.message.chat.id if event.message else None
        cb_data = event.data or ""

        # Выход в главное меню — история больше не нужна.
        if cb_data in ("menu:main", "menu:home"):
            await nav.forget(chat_id)
            return await handler(event, data)

        # Запоминаем источник ДО отрисовки экрана: клавиатура строится в
        # обработчике и должна видеть актуальную вершину стека.
        await nav.remember(chat_id, cb_data)
        result = await handler(event, data)

        # Перезеркалируем синхронное зеркало (обработчик мог вызвать
        # pop_until/forget внутри себя).
        if chat_id is not None:
            stack = await nav._load(int(chat_id))
            nav._mem[int(chat_id)] = stack
        return result
