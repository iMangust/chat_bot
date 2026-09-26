"""Универсальная постраничная inline-клавиатура (UX v1.4.9).

Единый стандарт для всех экранов с большим числом кнопок:
* до ``PAGE_SIZE`` (6) содержательных кнопок на страницу;
* layout 2 кнопки в ряд (длинные подписи — по одной, чтобы не резались);
* строка навигации ◀️ · «Название 📖 i/n» · ▶️ (перехлёст зацикливается);
* фиксированный нижний ряд ⬅️ Назад (+ опциональные 🏠 Меню / внешняя ссылка).

Callback-данные страниц: ``<prefix>:page:<n>`` — обработчик экрана принимает
необязательный параметр ``page`` и перерендеривает себя (см. shop/merch/stats).
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

PAGE_SIZE = 6          # максимум действий на страницу
NAV_ROW = "◀️"         # символ строки навигации


def _button_width(btn: InlineKeyboardButton) -> int:
    """Грубая ширина подписи: emoji считаются за 2 символа."""
    text = btn.text or ""
    visual = sum(2 if ord(ch) > 0x2190 else 1 for ch in text)
    return visual


def _chunk(rows: list[list[InlineKeyboardButton]], size: int) -> list[list[list[InlineKeyboardButton]]]:
    """Режет плоский список рядов на страницы по ~size кнопок, не разрывая ряды."""
    pages: list[list[list[InlineKeyboardButton]]] = []
    current: list[list[InlineKeyboardButton]] = []
    count = 0
    for row in rows:
        if current and count + len(row) > size:
            pages.append(current)
            current, count = [], 0
        current.append(row)
        count += len(row)
    if current:
        pages.append(current)
    return pages or [[]]


def paged_keyboard(
    buttons: list[InlineKeyboardButton],
    *,
    prefix: str,
    title: str = "",
    page: int = 0,
    page_size: int = PAGE_SIZE,
    back_cb: str | None = "menu:main",
    back_label: str = "⬅️ Назад",
    home_cb: str | None = None,
    url_button: InlineKeyboardButton | None = None,
) -> tuple[InlineKeyboardMarkup, int]:
    """Собирает постраничную клавиатуру.

    Возвращает ``(markup, page)`` — страница нормализована в диапазон
    ``[0, total_pages-1]``, чтобы вызывающий код мог синхронизировать свой
    рендер текста с фактической страницей кнопок.
    """
    buttons = [
        btn if btn.callback_data is not None or btn.url is not None
        else InlineKeyboardButton(text=btn.text, callback_data=None)
        for btn in buttons
    ]
    rows: list[list[InlineKeyboardButton]] = []
    line: list[InlineKeyboardButton] = []
    line_width = 0
    for btn in buttons:
        w = _button_width(btn)
        if w >= 24:                       # длинная подпись — всегда своя строка
            if line:
                rows.append(line)
                line, line_width = [], 0
            rows.append([btn])
            continue
        if line and line_width + w > 38:  # две длинные кнопки в ряд не влезут
            rows.append(line)
            line, line_width = [], 0
        line.append(btn)
        line_width += w
    if line:
        rows.append(line)

    pages = _chunk(rows, page_size)
    total = len(pages)
    page = max(0, min(page, total - 1))   # clamp вместо молчаливого «пусто»

    kb_rows: list[list[InlineKeyboardButton]] = [list(r) for r in pages[page]]

    # --- навигация -----------------------------------------------------
    prev_cb = f"{prefix}:page:{(page - 1) % total}"
    next_cb = f"{prefix}:page:{(page + 1) % total}"
    label = f"{title} 📖 {page + 1}/{total}".strip() if total > 1 else title.strip()
    nav: list[InlineKeyboardButton] = []
    if total > 1:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=prev_cb))
        nav.append(InlineKeyboardButton(text=label or "📖", callback_data=f"{prefix}:noop"))
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=next_cb))
    elif label:
        nav.append(InlineKeyboardButton(text=label, callback_data=f"{prefix}:noop"))
    if nav:
        kb_rows.append(nav)

    # --- фиксированный низ ---------------------------------------------
    footer: list[InlineKeyboardButton] = []
    if back_cb:
        footer.append(InlineKeyboardButton(text=back_label, callback_data=back_cb))
    if home_cb and home_cb != back_cb:
        footer.append(InlineKeyboardButton(text="🏠 Меню", callback_data=home_cb))
    if url_button is not None:
        kb_rows.append([url_button])
    if footer:
        kb_rows.append(footer)
    return InlineKeyboardMarkup(inline_keyboard=kb_rows), page
