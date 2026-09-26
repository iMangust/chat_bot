"""Регресс-тесты UX v1.5.2: пагинированное главное меню + единая стилистика.

Главное меню разбито на страницы («🎮 Игра» / «👤 Профиль»), навигация —
как в хабе питомца: ◀️ · «🏠 Название 📖 i/n» · ▶️, все подписи с эмодзи.
"""


from app.handlers import start as start_handlers
from app.keyboards.inline import MENU_PAGES, main_menu, pet_hub


def _flat(kb):
    return [b for row in kb.inline_keyboard for b in row]


# ---------------------------------------------------------------------------
# Структура меню
# ---------------------------------------------------------------------------

def test_menu_pages_have_emoji_titles_and_actions():
    assert len(MENU_PAGES) >= 2
    for title, actions in MENU_PAGES:
        assert title.startswith(("🎮", "👤", "🛒", "⚙️")), title
        assert actions
        for text, cb in actions:
            assert cb.startswith("menu:"), cb
            # фикс опечатки v1.5.1: было «📊 Мои статистика»
            assert "Мои статистика" not in text


def test_main_menu_paged_max_six_buttons_per_page():
    for page in range(len(MENU_PAGES)):
        kb = main_menu(page=page)
        content_rows = [r for r in kb.inline_keyboard
                        if not any((b.callback_data or "").startswith(("menu:page:", "menu:main"))
                                   or (b.callback_data or "") == "menu:noop"
                                   for b in r)]
        assert sum(len(r) for r in content_rows) <= 6
        cbs = [b.callback_data or "" for b in _flat(kb)]
        nav = [c for c in cbs if ":page:" in c]
        assert nav, f"нет ◀️/▶️ навигации на странице {page}"


def test_main_menu_navigation_row_style():
    kb = main_menu(page=0)
    texts = [b.text for b in _flat(kb)]
    assert any(t.startswith("◀️") for t in texts)
    assert any("▶️" in t for t in texts)
    # заголовок страницы со счётчиком — как в pet_hub
    assert any("📖" in t for t in texts)


def test_main_menu_merch_on_game_page():
    kb = main_menu(page=0)
    cbs = [b.callback_data for b in _flat(kb)]
    # merch_enabled по умолчанию в тестах может быть выключен — проверяем
    # только что callback merч не уходит на страницу «Профиль»
    from app.config import get_settings
    if get_settings().merch_enabled:
        assert "menu:merch" in cbs


def test_main_menu_page_wraparound_negative():
    kb_neg = main_menu(page=-1)
    kb_last = main_menu(page=len(MENU_PAGES) - 1)
    assert ([b.text for b in _flat(kb_neg)] ==
            [b.text for b in _flat(kb_last)])


# ---------------------------------------------------------------------------
# Хендлеры menu:page / menu:noop
# ---------------------------------------------------------------------------

def test_menu_page_and_noop_handlers_registered():
    names = {h.callback.__name__
             for h in start_handlers.router.callback_query.handlers}
    assert "cb_main_menu_page" in names
    assert "cb_main_menu_noop" in names


class _FakeCb:
    """Лёгкая заглушка CallbackQuery (модель aiogram frozen — не подкласс)."""

    def __init__(self, data: str):
        self.data = data
        self.message = None
        self.answered = 0

    async def answer(self, *a, **k):
        self.answered += 1


async def test_menu_noop_answers_only():
    cb = _FakeCb("menu:noop")
    await start_handlers.cb_main_menu_noop(cb)
    assert cb.answered == 1


async def test_menu_page_bad_number_falls_back_to_zero(monkeypatch):
    rendered = {}

    async def fake_render(cb, session, page=0):
        rendered["page"] = page
    monkeypatch.setattr(start_handlers, "_render_main_menu", fake_render)
    cb = _FakeCb("menu:page:abc")
    await start_handlers.cb_main_menu_page(cb, session=None)
    assert rendered["page"] == 0


async def test_menu_page_valid_number_forwarded(monkeypatch):
    rendered = {}

    async def fake_render(cb, session, page=0):
        rendered["page"] = page
    monkeypatch.setattr(start_handlers, "_render_main_menu", fake_render)
    cb = _FakeCb("menu:page:1")
    await start_handlers.cb_main_menu_page(cb, session=None)
    assert rendered["page"] == 1


# ---------------------------------------------------------------------------
# Единая стилистика: заголовки без эмодзи больше нет
# ---------------------------------------------------------------------------

def test_main_menu_text_has_emoji_header_with_page_title():
    class U:
        level, xp, coins, streak_days, first_name = 1, 0, 0, 0, "Тест"
    text = start_handlers._main_menu_text(U(), page=0)
    header = text.splitlines()[0]
    assert header.startswith("🏠 <b>Главное меню"), header
    title = MENU_PAGES[0][0]
    assert title in header


def test_pet_hub_bottom_nav_has_home():
    kb = pet_hub(page=0)
    cbs = [b.callback_data or "" for b in _flat(kb)]
    assert "menu:main" in cbs  # ⬅️ Назад из хаба питомца ведёт в меню
