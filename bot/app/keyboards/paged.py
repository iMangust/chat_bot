"""Универсальная постраничная inline-клавиатура (UX v1.4.9).

Единый стандарт для всех экранов с большим числом кнопок:
* до ``PAGE_SIZE`` (6) содержательных кнопок на страницу;
* layout 2 кнопки в ряд (длинные подписи — по одной, чтобы не резались);
* 4-й ряд — страницы: ◀️ · «Название 📖 i/n» · ▶️ (перехлёст зацикливается);
* фиксированный нижний ряд — ОДНА кнопка выхода 🏠 Меню (без дублей «Назад»).

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


def _two_per_row(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    """Раскладывает кнопки рядами ровно по две (стандарт навигации v1.5.3+)."""
    return [list(buttons[i:i + 2]) for i in range(0, len(buttons), 2)]


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
    home_cb: str | None = None,
    url_button: InlineKeyboardButton | None = None,
) -> tuple[InlineKeyboardMarkup, int]:
    """Собирает постраничную клавиатуру.

    Возвращает ``(markup, page)`` — страница нормализована в диапазон
    ``[0, total_pages-1]``, чтобы вызывающий код мог синхронизировать свой
    рендер текста с фактической страницей кнопок.
    """
    # служебные ряды (навигация/выход/url) добавляются после нарезки на
    # страницы — из подсчёта кнопок на страницу исключаем только их;
    # кнопки без callback_data/url некликабельны и в навигацию не попадают
    def _is_service(btn: InlineKeyboardButton) -> bool:
        cb = btn.callback_data or ""
        return (btn.url is not None or ":page:" in cb or ":noop" in cb
                or cb in ("menu:main", "menu:home") or btn.text == "🏠 Меню")

    content_buttons = [b for b in buttons if not _is_service(b)]
    rows = _two_per_row(content_buttons)

    pages = _chunk(rows, page_size)
    total = len(pages)
    if page < 0:                          # зацикливание: «предыдущая» с первой
        page %= total
    page = max(0, min(page, total - 1))   # clamp вместо молчаливого «пусто»

    kb_rows: list[list[InlineKeyboardButton]] = [list(r) for r in pages[page]]

    # --- строка страниц (4-й ряд): ◀️ · i/n · ▶️ -------------------------
    # Единая стилистика v1.5.3: «Назад» — это ◀️/▶️ по страницам; выход с
    # экрана — одна кнопка 🏠 Меню в самом низу (без дублей).
    prev_cb = f"{prefix}:page:{(page - 1) % total}"
    next_cb = f"{prefix}:page:{(page + 1) % total}"
    label = f"{title} 📖 {page + 1}/{total}".strip() if total > 1 else title.strip()
    if total > 1:
        kb_rows.append([
            InlineKeyboardButton(text="◀️", callback_data=prev_cb),
            InlineKeyboardButton(text=label or f"📖 {page + 1}/{total}",
                                 callback_data=f"{prefix}:noop"),
            InlineKeyboardButton(text="▶️", callback_data=next_cb),
        ])
    elif label:
        kb_rows.append([InlineKeyboardButton(text=label,
                                             callback_data=f"{prefix}:noop")])

    # --- фиксированный низ: одна кнопка выхода --------------------------
    if url_button is not None:
        kb_rows.append([url_button])
    exit_cb = home_cb or back_cb
    if exit_cb:
        kb_rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data=exit_cb)])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows), page
