from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

PAGE_SIZE = 6
NAV_ROW = "◀️"

BACK_LABEL = "⬅️ Назад"
HOME_LABEL = "🏠 Меню"

def _button_width(btn: InlineKeyboardButton) -> int:
    text = btn.text or ""
    visual = sum(2 if ord(ch) > 0x2190 else 1 for ch in text)
    return visual

def _two_per_row(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [list(buttons[i:i + 2]) for i in range(0, len(buttons), 2)]

def _chunk(rows: list[list[InlineKeyboardButton]], size: int) -> list[list[list[InlineKeyboardButton]]]:
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

def paged_pages(buttons: list[InlineKeyboardButton],
                page_size: int = PAGE_SIZE) -> list[list[list[InlineKeyboardButton]]]:
    def _is_service(btn: InlineKeyboardButton | None) -> bool:
        if btn is None:
            return False
        cb = btn.callback_data or ""
        return (btn.url is not None or ":page:" in cb or ":noop" in cb
                or cb in ("menu:main", "menu:home") or btn.text == "🏠 Меню")

    content = [b for b in buttons if not _is_service(b)]
    return _chunk(_two_per_row(content), page_size)

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
    pages: list[list[list[InlineKeyboardButton]]] | None = None,
) -> tuple[InlineKeyboardMarkup, int]:
    if pages is None:
        pages = paged_pages(buttons, page_size)

    total = len(pages)
    if page < 0:
        page %= total
    page = max(0, min(page, total - 1))

    kb_rows: list[list[InlineKeyboardButton]] = [list(r) for r in pages[page]]

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

    if url_button is not None:
        kb_rows.append([url_button])
    # Единая нижняя строка навигации: «⬅️ Назад» (в корень раздела/экран
    # источника) + «🏠 Меню» (главное меню). home_cb=None означает «домами
    # служит back_cb» — отдельная кнопка «Меню» тогда не нужна.
    nav_btns: list[InlineKeyboardButton] = []
    if back_cb:
        nav_btns.append(InlineKeyboardButton(text=BACK_LABEL, callback_data=back_cb))
    home_target = home_cb if home_cb is not None else back_cb
    if home_target and home_target != back_cb:
        nav_btns.append(InlineKeyboardButton(text=HOME_LABEL, callback_data=home_target))
    if nav_btns:
        kb_rows.append(nav_btns)
    return InlineKeyboardMarkup(inline_keyboard=kb_rows), page
