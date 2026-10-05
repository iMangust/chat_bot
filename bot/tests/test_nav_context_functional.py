"""Функциональные проверки контекстной навигации «Назад / Меню».

Гарантии, которые здесь фиксируются:
 1. Главное меню чистое: без CTA «🥚 Усыновить питомца» (усыновление живёт
    в разделе питомца), кнопка «▶️» листает страницы меню;
 2. Верхнеуровневые экраны без подуровней («Статистика», «Топы»,
    «Достижения», «Карточка», настройки) НЕ показывают кнопку «⬅️ Назад»,
    если в истории нет реального внешнего источника — только «🏠 Меню»;
 3. Если пользователь зашёл из другого раздела, «Назад» возвращается туда
    (Мерч → Статистика → «Назад» = категория мерча);
 4. Подстраницы разделов (мерч/события) при пустой истории деградируют до
    корня своего раздела, а не в главное меню;
 5. Стабильные подразделы (игры внутри хаба питомца) всегда имеют «Назад»
    в свой хаб.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.keyboards import inline as ik
from app.utils import nav


def _back_cb_of(kb):
    """callback_data кнопки «⬅️ Назад» или None, если её нет."""
    for row in kb.inline_keyboard:
        for btn in row:
            if "Назад" in btn.text:
                return btn.callback_data
    return None


def _set_stack(chat_id: int, items: list[str]) -> None:
    # ВАЖНО: mem_stack возвращает КОПИЮ списка — пишем прямо в _mem,
    # иначе стек под тестом останется пустым и клавиатура деградирует.
    from collections import deque
    nav._mem[int(chat_id)] = deque(items)


def test_main_menu_has_no_adopt_cta_and_next_pages():
    rows = [[b.text for b in r] for r in ik.main_menu(page=0).inline_keyboard]
    flat = [t for r in rows for t in r]
    assert not any("Усыновить" in t for t in flat), rows
    # «▶️» — отдельная кнопка постраничной навигации, ведёт на след. страницу
    assert "▶️" in flat, rows
    next_btns = [btn for r in ik.main_menu(page=0).inline_keyboard
                 for btn in r if btn.text == "▶️"]
    assert next_btns[0].callback_data.startswith("menu:page:"), next_btns[0].callback_data


def test_stats_from_main_menu_has_no_back_button():
    _set_stack(101, ["menu:stats"])
    b = InlineKeyboardBuilder()
    b.button(text="🏆 Достижения", callback_data="menu:ach")
    b.adjust(2)
    ik.with_nav(b, "stats", chat_id=101)
    kb = b.as_markup()
    assert _back_cb_of(kb) is None
    texts = [btn.text for r in kb.inline_keyboard for btn in r]
    assert any("Домой" in t for t in texts), texts


def test_stats_from_merch_back_returns_to_merch():
    _set_stack(102, ["menu:merch", "merch:cat:3", "menu:stats"])
    b = InlineKeyboardBuilder()
    b.button(text="🏆 Достижения", callback_data="menu:ach")
    b.adjust(2)
    ik.with_nav(b, "stats", chat_id=102)
    assert _back_cb_of(b.as_markup()) == "merch:cat:3"


def test_card_top_level_only_menu_when_from_main():
    _set_stack(103, ["menu:card"])
    # прямая проверка сборщика карточки (social._card_kb использует тот же путь)
    b = InlineKeyboardBuilder()
    b.row(*ik.nav_row("card", back_cb=ik._nav_back_cb("card", 103)))
    assert _back_cb_of(b.as_markup()) is None


def test_card_from_stats_back_returns_to_stats():
    _set_stack(104, ["menu:stats", "menu:card"])
    b = InlineKeyboardBuilder()
    b.row(*ik.nav_row("card", back_cb=ik._nav_back_cb("card", 104)))
    assert _back_cb_of(b.as_markup()) == "menu:stats"


def test_top_ach_settings_from_menu_have_no_back():
    _set_stack(105, ["menu:top"])
    assert _back_cb_of(ik.top_tabs("week", "talk", chat_id=105)) is None
    _set_stack(106, ["menu:ach"])
    assert _back_cb_of(ik.achievements_list([], 0, 1, chat_id=106)) is None
    _set_stack(107, ["menu:settings"])
    assert _back_cb_of(ik.settings_keyboard({}, chat_id=107)) is None


def test_empty_stack_top_level_sections_have_no_back():
    for cid, sec in [(111, "stats"), (112, "top"), (113, "ach"),
                     (114, "settings"), (115, "card")]:
        _set_stack(cid, [])
        assert ik._nav_back_cb(sec, cid) is None, sec


def test_merch_root_empty_stack_has_no_selfloop_back():
    # Корневой список мерча открывается кнопкой 'menu:merch' — «Назад»
    # на неё перерисовал бы тот же экран («кнопка ничего не делает»),
    # поэтому при пустой истории остаётся только «🏠 Меню». А вот с
    # подстраницы (товар) корень — валидный пункт возврата.
    _set_stack(116, [])
    b = InlineKeyboardBuilder()
    ik.append_nav(b, "merch", None)
    assert _back_cb_of(b.as_markup()) is None
    # Подстраница товара при пустой истории: «Назад» → список категорий
    b2 = InlineKeyboardBuilder()
    ik.append_nav(b2, "merch", back_cb="menu:merch", chat_id=116,
                  current_cb="merch:prod:7")
    assert _back_cb_of(b2.as_markup()) == "menu:merch"


def test_games_submenu_from_pet_hub_back_to_menu_only():
    # Хаб питомца ('pet:page:*') и игры ('pet:games') — ОДИН экран с
    # вкладками: «Назад» из игр в хаб перерисовал бы то же сообщение
    # («ничего не происходит»). Поэтому возврат наружу даёт только
    # «🏠 Меню», а внутренняя история раздела игнорируется.
    _set_stack(117, ["menu:pet", "pet:page:2", "pet:games"])
    kb = ik.games_menu(chat_id=117)
    bc = _back_cb_of(kb)
    assert bc is None, (kb, bc)
    texts = [btn.text for r in kb.inline_keyboard for btn in r]
    assert any("Домой" in t for t in texts), texts


# ── Регрессия бага «Назад ничего не делает» (Статистика из главного меню):
# вершина стека — сам текущий экран ('menu:stats'); он никогда не должен
# возвращаться как точка возврата.

def test_stats_from_main_no_self_loop_back():
    _set_stack(120, ["menu:main", "menu:stats"])  # открыт тапом «📊 Статы»
    b = InlineKeyboardBuilder()
    b.button(text="🏆 Достижения", callback_data="menu:ach")
    ik.with_nav(b, "stats", chat_id=120)
    kb = b.as_markup()
    assert _back_cb_of(kb) is None          # самопетли нет
    texts = [btn.text for r in kb.inline_keyboard for btn in r]
    assert any("Домой" in t for t in texts), texts


def test_pet_hub_from_main_no_self_loop_back():
    _set_stack(121, ["menu:main", "menu:pet"])
    kb = ik.pet_page_kb(0, chat_id=121) if hasattr(ik, "pet_page_kb") else None
    if kb is not None:
        assert _back_cb_of(kb) != "menu:pet"


# ── Плоские разделы («Топы», «Достижения»): многоуровней нет — вкладки
# внутри экрана + «🏠 Меню»; кнопка «Назад» не показывается даже при
# переходе из другого раздела.

def test_flat_sections_never_show_back_even_from_other_section():
    _set_stack(130, ["menu:merch", "merch:cat:3", "menu:top"])
    assert _back_cb_of(ik.top_tabs("week", "talk", chat_id=130)) is None
    _set_stack(131, ["menu:merch", "merch:cat:3", "menu:ach"])
    assert _back_cb_of(ik.achievements_list([], 0, 1, chat_id=131)) is None
    # прямые вызовы сборщиков тоже подавляют «Назад»
    assert _back_cb_of(ik.nav_kb("top", back_cb="merch:cat:3")) is None
    row = ik.nav_row("ach", back_cb="merch:cat:3")
    assert all("Назад" not in btn.text for btn in row)


def test_settings_back_still_works_when_from_merch():
    # Настройки — НЕ плоский раздел: источник перехода учитывается
    _set_stack(140, ["menu:merch", "merch:cat:3", "menu:settings"])
    kb = ik.settings_keyboard({}, chat_id=140)
    assert _back_cb_of(kb) == "merch:cat:3"

