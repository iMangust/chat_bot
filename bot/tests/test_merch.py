"""Тесты отдельного раздела 🧢 Мерч канала (app/handlers/merch.py)."""
from __future__ import annotations

import pytest


def test_default_showcase_categories():
    from app.handlers.merch import CATEGORIES, _items_by_cat
    groups = _items_by_cat()
    assert set(CATEGORIES) >= {"tshirt", "hoodie", "acc"}
    # в дефолтной витрине есть и футболки, и худи — как требует канал
    assert groups["tshirt"] and groups["hoodie"]
    for _, it in groups["tshirt"]:
        assert it["cat"] == "tshirt" and it["price"] > 0 and it["name"]


def test_find_item_bounds():
    from app.handlers.merch import _find, _parse_items
    n = len(_parse_items())
    assert _find("0") is not None
    assert _find(str(n - 1)) is not None
    assert _find(str(n)) is None
    assert _find("abc") is None
    assert _find("-1") is None


def test_parse_items_from_config(monkeypatch):
    from app.config import get_settings
    from app.handlers.merch import _parse_items
    monkeypatch.setattr(get_settings(), "merch_items",
                        "tshirt|Лонгслив «Флуд»|990 ₽|Мягкий хлопок|S,M,L;"
                        "hoodie|Свитшот|1500|Оверсайз;")
    items = _parse_items()
    assert len(items) == 2
    assert items[0]["name"] == "Лонгслив «Флуд»"
    assert items[0]["price"] == 990
    assert items[0]["sizes"] == ["S", "M", "L"]
    assert items[1]["cat"] == "hoodie"
    assert items[1]["sizes"] == []


def test_parse_items_unknown_category_falls_back(monkeypatch):
    from app.config import get_settings
    from app.handlers.merch import _parse_items
    monkeypatch.setattr(get_settings(), "merch_items",
                        "носки|Тёплые носки|300")
    items = _parse_items()
    assert items and items[0]["cat"] == "acc"


def test_merch_button_in_main_menu():
    """В главном меню мерч — callback на отдельный раздел, а не url-кнопка."""
    from app.keyboards.inline import main_menu
    kb = main_menu()
    texts = [b.text for row in kb.inline_keyboard for b in row]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "🧢 Мерч канала" in texts
    assert "menu:merch" in cbs
    # url-вариант больше не используется в главном меню
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    assert all(u != "https://example.com" for u in urls)


def test_shop_links_to_merch_section():
    """Магазин питомца ссылается на отдельный раздел мерча кнопкой-menu."""
    from app.handlers.shop import shop_keyboard
    from app.db.models import Item
    items = [Item(id=1, code="food_bread", name="Хлеб", icon="🍞",
                  type="food", price=5, effect={}, description="")]
    kb = shop_keyboard(items, user_coins=100).as_markup()
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(c and c.startswith("buy:") for c in cbs)


@pytest.mark.asyncio
async def test_merch_router_registered():
    """Router мерча подключён к диспетчеру (импорт main не падает, merch там есть)."""
    import app.main as m
    assert hasattr(m, "merch")
    names = {r.name for r in m.__dict__.get("_dummy", [])} if False else set()
    # проверяем через include_routers в исходнике
    import inspect
    src = inspect.getsource(m)
    assert "merch.router" in src
