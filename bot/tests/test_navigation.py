"""Навигация меню: пагинация 2x3, ряд ◀️ i/n ▶️, один выход."""
from __future__ import annotations

from _helpers import _btns, _content_rows, get_settings

from app.keyboards import inline as ikb
from app.keyboards.paged import paged_keyboard

# ---------------------------------------------------------------------------
# 1. Навигация
# ---------------------------------------------------------------------------
class TestNavigation:
    def test_main_menu_pages_defined(self):
        assert len(ikb.MENU_PAGES) >= 2
        for title, buttons in ikb.MENU_PAGES:
            assert title.startswith(("🎮", "👤"))
            assert 0 < len(buttons) <= 6

    def test_main_menu_two_per_row_and_one_exit(self):
        kb = ikb.main_menu(link="https://t.me/x", reward=50)
        for r in _content_rows(kb):
            assert len(r) <= 2, "контентные кнопки — строго по 2 в ряд"
        texts = [b.text for b in _btns(kb)]
        assert texts.count("🏠 Меню") == 1, "ровно один выход из меню"
        assert not any(t.startswith("⬅️") for t in texts), "дублей «Назад» быть не должно"

    def test_main_menu_nav_row_counter(self):
        total = len(ikb.MENU_PAGES)
        for page in range(total):
            kb = ikb.main_menu(link="l", reward=1, page=page)
            nav = [b for b in _btns(kb)
                   if (b.callback_data or "").startswith("menu:page:")]
            assert len(nav) == 2, "◀️ и ▶️ на месте"
            label = [b for b in _btns(kb) if (b.callback_data or "") == "menu:noop"]
            assert label and f"📖 {page + 1}/{total}" in label[0].text

    def test_main_menu_pagination_wraps(self):
        total = len(ikb.MENU_PAGES)
        kb = ikb.main_menu(link="l", reward=1, page=0)
        nav = [b.callback_data for b in _btns(kb)
               if (b.callback_data or "").startswith("menu:page:")]
        assert nav == [f"menu:page:{total - 1}", "menu:page:1"], \
            "зацикливание: ◀️ с первой страницы ведёт на последнюю"
        kb_last = ikb.main_menu(link="l", reward=1, page=total - 1)
        nav_last = [b.callback_data for b in _btns(kb_last)
                    if (b.callback_data or "").startswith("menu:page:")]
        assert nav_last == [f"menu:page:{total - 2}", "menu:page:0"]

    def test_merch_button_only_when_enabled(self):
        st = get_settings()
        was = getattr(st, "merch_enabled", True)
        try:
            st.merch_enabled = False
            kb = ikb.main_menu(link="l", reward=1, page=0)
            assert not any("мерч" in b.text.lower() for b in _btns(kb))
        finally:
            st.merch_enabled = was

    def test_pet_hub_navigation(self):
        kb = ikb.pet_hub(page=0)
        texts = [b.text for b in _btns(kb)]
        assert texts.count("🏠 Меню") == 1
        assert any("📖" in t for t in texts)
        for r in _content_rows(kb):
            assert len(r) <= 2

    def test_paged_keyboard_contract(self):
        from aiogram.types import InlineKeyboardButton as B
        items = [B(text=f"c{i}", callback_data=f"cb{i}") for i in range(12)]
        kb, page = paged_keyboard(items, title="🧪 Тест", prefix="t", page=0,
                                  page_size=4)
        btns = _btns(kb)
        assert page == 0
        assert any("📖 1/" in b.text for b in btns)
        assert sum(1 for b in btns if b.text == "🏠 Меню") == 1
        for r in _content_rows(kb):
            assert len(r) <= 2

    def test_no_double_back_in_all_keyboards(self):
        kbs = [ikb.main_menu(link="l", reward=1), ikb.pet_hub(page=0),
               ikb.back_to_main(), ikb.settings_keyboard({"daily_report": True})]
        for kb in kbs:
            texts = [b.text for b in _btns(kb)]
            assert texts.count("🏠 Меню") <= 1
