"""Регресс-тесты UX v1.4.9: постраничная навигация, ≤6 кнопок/страница."""
import pytest
from aiogram.types import InlineKeyboardButton

from app.keyboards.paged import paged_keyboard, PAGE_SIZE


def _btns(n):
    return [InlineKeyboardButton(text=f"⚪ Кнопка {i}", callback_data=f"x:{i}")
            for i in range(n)]


def _flat(kb):
    return [b for row in kb.inline_keyboard for b in row]


def test_single_page_no_arrows():
    kb, page = paged_keyboard(_btns(3), prefix="x", title="🧰 Тест")
    assert page == 0
    texts = [b.text for b in _flat(kb)]
    assert not any("▶️" in t or "◀️" in t for t in texts)
    assert "🧰 Тест" in texts  # заголовок-подпись без счётчика на 1 стр.


def test_paging_max_six_content_buttons():
    kb, page = paged_keyboard(_btns(15), prefix="x", title="🧰", page=1)
    content = [b for b in kb.inline_keyboard[0] if (b.callback_data or "").startswith("x:")]
    assert len(content) <= PAGE_SIZE
    assert page == 1
    nav = [b.callback_data for b in _flat(kb) if b.callback_data and ":page:" in b.callback_data]
    assert "x:page:0" in nav and "x:page:2" in nav


def test_page_clamped_and_wraps():
    kb, page = paged_keyboard(_btns(15), prefix="x", title="🧰", page=99)
    assert page == 2                       # clamp к последней странице
    nav_texts = {b.text: b.callback_data for b in _flat(kb)}
    assert nav_texts.get("Вперёд ▶️") == "x:page:0"   # зацикление
    assert nav_texts.get("◀️ Назад") == "x:page:1"


def test_footer_back_home_url():
    url = InlineKeyboardButton(text="🌐 Сайт", url="https://example.com")
    kb, _ = paged_keyboard(_btns(2), prefix="x", back_cb="menu:pet",
                           home_cb="menu:main", url_button=url)
    texts = [b.text for b in _flat(kb)]
    assert "⬅️ Назад" in texts and "🏠 Меню" in texts and "🌐 Сайт" in texts


def test_long_labels_get_own_row():
    long = InlineKeyboardButton(text="🎯 Использовать 🍰 Очень Длинное Название Товара ×12",
                                callback_data="use:1")
    short1 = InlineKeyboardButton(text="🍎 Яблоко · 10🪙", callback_data="buy:1")
    short2 = InlineKeyboardButton(text="🥩 Стейк · 25🪙", callback_data="buy:2")
    kb, _ = paged_keyboard([long, short1, short2], prefix="shop", title="🛒")
    rows = kb.inline_keyboard
    assert [b.text for b in rows[0]] == [long.text]      # длинная — сама в ряду
    assert [b.text for b in rows[1]] == [short1.text, short2.text]


def test_noop_label_clickable_handler_prefix():
    kb, _ = paged_keyboard(_btns(15), prefix="ach", title="🏆")
    noops = [b.callback_data for b in _flat(kb) if (b.callback_data or "").endswith(":noop")]
    assert noops == ["ach:noop"]


# ---------------- интеграция с роутерами ----------------

@pytest.mark.asyncio
async def test_shop_router_handles_page_callbacks():
    from app.handlers.shop import router as shop_router
    handlers = [h for h in shop_router.callback_query.handlers]
    datas = []
    for h in handlers:
        f = h.callback
        # извлекаем фильтры F.data
        try:
            from aiogram.filters import Filter
            pass
        except Exception:
            pass
    # проще: проверить наличие обработчиков через имена функций
    names = {h.callback.__name__ for h in handlers}
    assert "shop_noop" in names
    src = open("app/handlers/shop.py").read()
    assert 'F.data.startswith("shop:page:")' in src
    assert 'F.data.startswith("inv:page:")' in src


@pytest.mark.asyncio
async def test_merch_router_handles_category_pages():
    src = open("app/handlers/merch.py").read()
    assert 'F.data.startswith("merch:page:")' in src
    assert "_category_kb" in src
    # кнопки категорий подписаны эмодзи и количеством
    assert "шт." in src


def test_pet_hub_arrows_are_labeled():
    from app.keyboards.inline import pet_hub
    kb = pet_hub(0)
    texts = [b.text for b in _flat(kb)]
    assert any(t.startswith("◀️ ") for t in texts)
    assert any(t.endswith(" ▶️") for t in texts)


def test_achievements_arrows_labeled():
    from app.keyboards.inline import achievements_list
    kb = achievements_list([], page=0, total_pages=3)
    texts = [b.text for b in _flat(kb)]
    assert "◀️ Новее" in texts and "Старше ▶️" in texts


def test_top_tabs_arrows_labeled():
    from app.keyboards.inline import top_tabs
    kb = top_tabs("week", "talk")
    texts = [b.text for b in _flat(kb)]
    assert "◀️ Раздел" in texts and "Раздел ▶️" in texts
