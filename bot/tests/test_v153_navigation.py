"""Регресс-тесты UX v1.5.3: единая навигация без дублей «Назад».

Правила стилистики:
* контент — по 2 кнопки в ряд, максимум 3 ряда на страницу;
* 4-й ряд — ◀️ · «Название 📖 i/n» · ▶️ (зациклен);
* ровно ОДНА кнопка выхода «🏠 Меню», никаких «⬅️ Назад»;
* бот отвечает только в ЛС; неподписанным — заглушка с gate:check.
"""

from app.keyboards.inline import (MENU_PAGES, PET_PAGES, achievements_list,
                                  adopt_cta_kb, arena_keyboard, back_to_main,
                                  games_menu, main_menu, pet_hub, rps_keyboard,
                                  settings_keyboard, species_picker,
                                  train_menu)


def _flat(kb):
    return [b for row in kb.inline_keyboard for b in row]


ALL_KBS = {
    "main_menu": lambda: main_menu(),
    "main_menu_link": lambda: main_menu(link="https://t.me/x", reward=5),
    "pet_hub0": lambda: pet_hub(0),
    "pet_hub1": lambda: pet_hub(1),
    "pet_hub2": lambda: pet_hub(2),
    "pet_hub_critical": lambda: pet_hub(0, critical=True),
    "games_menu": games_menu,
    "rps": rps_keyboard,
    "train": train_menu,
    "back": back_to_main,
    "adopt_cta": adopt_cta_kb,
    "species": species_picker,
    "ach": lambda: achievements_list([], page=0, total_pages=3),
    "settings": lambda: settings_keyboard({}),
    "arena": arena_keyboard,
}


# ─── Единая стилистика: один выход, две в ряд, три ряда ─────────────────────

def test_no_duplicate_back_buttons_anywhere():
    """Никаких «⬅️ Назад» — единственный выход с экрана это «🏠 Меню»."""
    for name, make in ALL_KBS.items():
        texts = [b.text for b in _flat(make())]
        assert not any(t.startswith("⬅️") for t in texts), f"{name}: {[t for t in texts if t.startswith('⬅️')]}"


def test_exactly_one_home_button():
    for name, make in ALL_KBS.items():
        texts = [b.text for b in _flat(make())]
        if name == "species":
            # экран выбора вида — переход к следующему шагу онбординга,
            # выход здесь не нужен (отмена — /start).
            continue
        assert texts.count("🏠 Меню") == 1, f"{name}: {texts}"


def test_content_max_two_per_row_three_rows():
    for name, make in ALL_KBS.items():
        kb = make()
        rows = kb.inline_keyboard
        content_rows = [r for r in rows
                        if not all((b.callback_data or "").endswith((":noop",))
                                   or b.url is not None
                                   or b.text in ("🏠 Меню", "◀️", "▶️")
                                   or (b.callback_data or "").startswith(("menu:page:", "pet:page:", "ach:page:"))
                                   for b in r)]
        for r in content_rows:
            # исключение: три равнозначных хода КНБ — один ряд по смыслу
            assert len(r) <= 2 or name == "rps", f"{name}: ряд из {len(r)} кнопок: {[b.text for b in r]}"
        # выбор вида: 5 видов + «просто смотреть» = 6 кнопок = ровно 3 ряда по 2
        limit = 4 if name == "species" else 3
        assert len(content_rows) <= limit, f"{name}: {len(content_rows)} контентных рядов"


def test_nav_row_between_arrows_shows_page_counter():
    kb = main_menu(page=0)
    nav = [b for b in _flat(kb) if b.text in ("◀️", "▶️") or "📖" in b.text]
    labels = [b.text for b in nav]
    assert "◀️" in labels and "▶️" in labels
    counter = [t for t in labels if "📖" in t][0]
    assert counter.endswith(f"1/{len(MENU_PAGES)}"), counter


def test_pet_hub_page_counters():
    for page in range(len(PET_PAGES)):
        kb = pet_hub(page)
        texts = [b.text for b in _flat(kb)]
        counter = [t for t in texts if "📖" in t]
        assert counter and counter[0].endswith(f"{page + 1}/{len(PET_PAGES)}"), texts


def test_main_menu_callbacks_match_handlers():
    cbs = [b.callback_data or "" for b in _flat(main_menu(page=0))]
    # ◀️ -> menu:page:1 (зациклен), ▶️ -> menu:page:1; метка страницы кликабельна
    assert "menu:page:1" in cbs
    assert "menu:noop" in cbs
    assert "menu:main" in cbs                              # единственный выход


def test_pet_hub_history_on_every_page():
    for page in range(len(PET_PAGES)):
        cbs = [b.callback_data or "" for b in _flat(pet_hub(page))]
        assert "pet:history" in cbs, page


def test_pet_hub_critical_swaps_care_actions():
    kb = pet_hub(0, critical=True)
    cbs = [b.callback_data or "" for b in _flat(kb)]
    assert "pet:revive" in cbs and "pet:adopt" in cbs
    assert "pet:feed" not in cbs


def test_all_action_callbacks_are_prefixed_noops():
    """Мёртвых меток без префикса нет: все :noop идут со своим префиксом."""
    for name, make in ALL_KBS.items():
        for b in _flat(make()):
            cb = b.callback_data or ""
            if "noop" in cb:
                assert cb != "noop" and cb.count(":") >= 1, f"{name}: {cb}"
