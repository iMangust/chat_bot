"""Безопасное редактирование сообщений ботом.

Проблемы, которые решает этот модуль:

1. ``TelegramBadRequest: there is no text in the message to edit`` —
   возникает, когда бот пытается вызвать ``edit_text`` на сообщении без
   текста (фото/видео/кружок/стикер) или на пустом медиа-сообщении
   (например, пользователь переслал картинку и нажал inline-кнопку под ней).
2. ``Message is not modified`` — редактирование идентичным текстом.
3. Потеря «контекста экрана»: при ошибке редактирования падает весь
   апдейт, и пользователь не может никуда вернуться.

Стратегия: пробуем отредактировать; если сообщение не содержит текста
(или текст не изменился, или редактирование невозможно по другой причине) —
молча отправляем НОВОЕ сообщение с тем же содержимым. Так навигация бота
никогда не «ломается» на медиа-сообщениях.
"""
from __future__ import annotations

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, Message
from loguru import logger

# Явный HTML по умолчанию для всех исходящих сообщений.
# Важно: аргумент parse_mode=None при вызове edit_text/answer НЕ «наследует»
# default из DefaultBotProperties — aiogram передаёт None напрямую в Telegram
# API и клиент показывает сырые теги (<b>...</b>) вместо жирного текста.
DEFAULT_PARSE_MODE = ParseMode.HTML


def _has_editable_text(message: Message) -> bool:
    """True, если в сообщении есть непустой текст (caption тоже считается)."""
    text = (message.text or message.caption or "").strip()
    return bool(text)


async def safe_edit_or_answer(
    target: Message,
    text: str,
    *,
    reply_markup=None,
    parse_mode: str | None = "HTML",
    **kwargs,
) -> Message:
    """Редактирует ``target``, а если нельзя — шлёт новое сообщение.

    Возвращает итоговое сообщение (отредактированное или новое), чтобы
    вызывающий код мог продолжить работу с ним.

    ``parse_mode`` по умолчанию — HTML: без него aiogram передаёт ``None``
    напрямую в API и Telegram показывает сырые теги (<b>...</b>) вместо
    жирного текста.
    """
    if _has_editable_text(target):
        try:
            return await target.edit_text(
                text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs
            )
        except TelegramBadRequest as exc:
            msg = str(exc).lower()
            if "not modified" in msg:
                return target  # текст идентичен — ничего делать не нужно
            if "no text" in msg or "message can't be edited" in msg:
                pass  # падаем вниз — отправим новое сообщение
            else:
                logger.debug("edit_text failed ({}), sending new message", exc)
        except TelegramAPIError as exc:
            logger.debug("edit_text api error ({}), sending new message", exc)
    return await target.answer(text, reply_markup=reply_markup,
                               parse_mode=parse_mode, **kwargs)


async def answer_cb(cb: CallbackQuery, text: str, *, reply_markup=None,
                    parse_mode: str | None = "HTML", **kwargs) -> None:
    """Сокращение: безопасный ответ на callback (edit → fallback answer)."""
    if cb.message is None:
        return
    await safe_edit_or_answer(cb.message, text, reply_markup=reply_markup,
                               parse_mode=parse_mode, **kwargs)
