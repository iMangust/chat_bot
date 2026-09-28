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
    # ПРЕ-ФИЛЬТР (продакшен-баг v1.5.28: TelegramBadRequest в 15:23 при живом
    # /help): edit_text НЕ имеет механизма нарезки — если рендер экрана вышел
    # за 4096, редактирование гарантированно падает. Делим заранее: первым
    # сообщением РЕДАКТИРУЕМ первый чанк (с кнопками), остальное досылаем.
    from app.utils.text_split import split_message
    chunks = split_message(text)
    if len(chunks) > 1 and _has_editable_text(target):
        last = target
        try:
            last = await target.edit_text(
                chunks[0], reply_markup=reply_markup, parse_mode=parse_mode,
                **kwargs)
        except TelegramBadRequest as exc:
            low = str(exc).lower()
            if "not modified" not in low:
                logger.debug("pre-split edit failed ({}), sending new", exc)
                last = None  # уйдём в обычную ветку отправки ниже
        if last is not None:
            for chunk in chunks[1:]:
                try:
                    last = await target.answer(chunk, parse_mode=parse_mode,
                                               **kwargs)
                except TelegramBadRequest:
                    last = await target.answer(_strip_tags(chunk))
            return last

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
            elif "new text provided" in msg:
                # Telegram: «The new text provided is too long» — редактировать
                # длинное сообщение нельзя, но ОТПРАВИТЬ его по частям можно.
                from app.utils.text_split import split_message
                chunks = split_message(text)
                last = target
                for i, chunk in enumerate(chunks):
                    try:
                        last = await target.answer(
                            chunk,
                            reply_markup=reply_markup if i == len(chunks) - 1 else None,
                            parse_mode=parse_mode, **kwargs)
                    except TelegramBadRequest:
                        # fallback: без разметки (сломанный HTML у вызывающего)
                        last = await target.answer(
                            _strip_tags(chunk),
                            reply_markup=reply_markup if i == len(chunks) - 1 else None)
                return last
            else:
                logger.debug("edit_text failed ({}), sending new message", exc)
        except TelegramAPIError as exc:
            logger.debug("edit_text api error ({}), sending new message", exc)
    # Финальная отправка тоже может быть слишком длинной или с битым HTML —
    # режем на части и при отказе парсера дублируем plain-текстом, чтобы
    # экран никогда не «падал» молча (см. баг /help → TelegramBadRequest).
    from app.utils.text_split import split_message
    last = target
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        try:
            last = await target.answer(chunk, reply_markup=markup,
                                       parse_mode=parse_mode, **kwargs)
        except TelegramBadRequest as exc:
            low = str(exc).lower()
            if "can't parse" in low or "entity" in low or "tag" in low:
                logger.debug("answer HTML broken ({}), sending plain text", exc)
                last = await target.answer(_strip_tags(chunk), reply_markup=markup)
            else:
                raise
    return last


_TAG_RE = None  # ленивая компиляция


def _strip_tags(text: str) -> str:
    global _TAG_RE
    if _TAG_RE is None:
        import re
        _TAG_RE = re.compile(r"</?(b|i|u|s|code|pre|a|tg-spoiler)[^>]*>")
    return _TAG_RE.sub("", text)


def _looks_like_html_error(exc: BaseException) -> bool:
    """Telegram BadRequest про сломанную разметку (в т.ч. edit_text)."""
    low = str(exc).lower()
    return ("can't parse" in low or "unsupported start tag" in low
            or "unbalanced" in low or "entity" in low or "tag" in low)


async def safe_edit_html(
    target: Message,
    text: str,
    *,
    reply_markup=None,
    parse_mode: str | None = "HTML",
    **kwargs,
) -> Message:
    """edit_text с гарантированным фолбэком на битом HTML (v1.5.65).

    Продакшен-баг: карточка питомца содержала неэкранированный «<»
    («Настроение: <b>50.</b>» — Telegram видел открывающий тег «50.»),
    edit_text падал с BadRequest, и пользователь получал тост
    «Упс, что-то пошло не так». Здесь: при ошибке парсинга Entities
    редактируем очищенным от тегов текстом — экран никогда не «ломается»,
    а текст остаётся читаемым.
    """
    from app.utils.text_split import split_message
    chunks = split_message(text)
    last = target
    if _has_editable_text(target):
        try:
            last = await target.edit_text(
                chunks[0], reply_markup=reply_markup, parse_mode=parse_mode,
                **kwargs)
            for chunk in chunks[1:]:
                last = await target.answer(chunk, parse_mode=parse_mode,
                                           **kwargs)
            return last
        except TelegramBadRequest as exc:
            msg = str(exc).lower()
            if "not modified" in msg:
                return target
            if _looks_like_html_error(exc) and parse_mode == ParseMode.HTML:
                logger.debug("edit HTML broken ({}), editing plain text", exc)
                clean = _strip_tags(text)
                clean_chunks = split_message(clean)
                try:
                    last = await target.edit_text(
                        clean_chunks[0], reply_markup=reply_markup,
                        parse_mode=None, **kwargs)
                    for chunk in clean_chunks[1:]:
                        last = await target.answer(chunk, parse_mode=None,
                                                   **kwargs)
                    return last
                except TelegramAPIError as exc2:
                    logger.debug("plain edit failed too ({})", exc2)
            elif "no text" not in msg and "message can't be edited" not in msg \
                    and "new text provided" not in msg:
                logger.debug("edit_text failed ({}), sending new message", exc)
    # обычный путь: отредактировать нельзя → новая отправка со всеми фолбэками
    return await safe_edit_or_answer(target, text, reply_markup=reply_markup,
                                     parse_mode=parse_mode, **kwargs)


async def answer_safe(message: Message, text: str, *, reply_markup=None,
                      parse_mode: str | None = "HTML", **kwargs) -> Message:
    """Безопасный message.answer: режет текст >4096 и чинит битый HTML.

    Прямые `message.answer(...)` в текстовых командах (/pet, /stats…) падали
    с TelegramBadRequest, если рендер превышал лимит или разметка была битой.
    """
    from app.utils.text_split import split_message
    last = message
    for i, chunk in enumerate(split_message(text)):
        markup = reply_markup if i == len(split_message(text)) - 1 else None
        try:
            last = await message.answer(chunk, reply_markup=markup,
                                        parse_mode=parse_mode, **kwargs)
        except TelegramBadRequest as exc:
            low = str(exc).lower()
            if "can't parse" in low or "entity" in low or "tag" in low:
                logger.debug("answer HTML broken ({}), sending plain text", exc)
                last = await message.answer(_strip_tags(chunk), reply_markup=markup)
            else:
                raise
    return last


async def answer_cb(cb: CallbackQuery, text: str, *, reply_markup=None,
                    parse_mode: str | None = "HTML", **kwargs) -> None:
    """Сокращение: безопасный ответ на callback (edit → fallback answer)."""
    if cb.message is None:
        return
    await safe_edit_or_answer(cb.message, text, reply_markup=reply_markup,
                               parse_mode=parse_mode, **kwargs)
