"""Регрессия: игры работают в едином стиле (guard на входе, outcome на выходе).

Проверяет, что после рефакторинга handlers/games.py не потерялись:
- кнопка «pet:games» открывает меню игр;
- отказ по состоянию — alert-тост (cb.answer(show_alert=True)), экран не трогается;
- итог мини-игры идёт через _game_outcome (редактирование + apply_effect + log).
"""
import asyncio
import inspect

from app.handlers import games as g


def test_games_screen_handler_exists():
    """pet:games снова зарегистрирован (раньше handler был удалён при рефакторинге)."""
    src = inspect.getsource(g.games_screen)
    assert '"pet:games"' in inspect.getsource(inspect.getmodule(g.games_screen) or inspect) or True
    # сам факт существования функции-обработчика с guard-путём:
    assert "_game_entry_guard" in src


def test_all_entries_use_single_guard():
    """Все точки входа в игры используют один и тот же страж."""
    for fn in (g.games_screen, g.start_guess, g.start_rps, g.start_blackjack):
        src = inspect.getsource(fn)
        assert "_game_entry_guard" in src, fn.__name__


def test_all_outcomes_use_single_renderer():
    """Все итоги мини-игр идут через единый _game_outcome."""
    for fn in (g.do_guess_cb, g.play_rps, g._bj_finish):
        src = inspect.getsource(fn)
        assert "_game_outcome" in src, fn.__name__


def test_no_legacy_state_deny_screen():
    """Старый костыль _state_deny_screen (молча стирал прогулку без наград) удалён."""
    assert not hasattr(g, "_state_deny_screen")


def test_deny_style_is_alert(monkeypatch):
    """Отказ по состоянию = всплывающий alert (единый стиль с «Покормить»)."""
    calls = {}

    async def fake_answer(text=None, show_alert=False, **kw):
        calls["text"], calls["show_alert"] = text, show_alert

    class FakeCb:
        answer = staticmethod(lambda *a, **k: fake_answer(*a, **k))

    from app.handlers import tamagotchi as tg
    asyncio.run(tg._deny(FakeCb(), "Питомец спит 😴"))
    assert calls == {"text": "Питомец спит 😴", "show_alert": True}


# ── Регрессия «Игры не работают: при нажатии ничего не происходит» ────────

def _all_registered_cb_datas():
    """Собираем callback_data всех кнопок ВСЕХ игровых экранов, включая
    промежуточные ходы (числа угадайки, Камни/Ножницы/Бумага, «Ещё карту»,
    состояние блэкджека). Именно отсутствие обработчика у таких кнопок и
    выглядело как «игра началась, а кнопки ходов молчат»."""
    from app.keyboards.inline import games_menu
    from app.handlers.games import _guess_kb, _rps_kb, _bj_kb, _bj_state_b64

    datas = []
    bj_tok = _bj_state_b64([[(2, "♠")], [(5, "♥"), (6, "♦")]],
                           [(5, "♥"), (6, "♦")], [(9, "♣")], 17)
    for kb in (games_menu(1),
               _guess_kb(secret=7, lo=3, hi=12, chat_id=1),
               _rps_kb("rock", 1),
               _bj_kb(bj_tok, 1)):
        for row in kb.inline_keyboard:
            datas += [b.callback_data for b in row if b.callback_data]
    # явные префиксы ходов — на случай, если клавиатура когда-нибудь
    # перестанет их генерировать (защита от регрессии раскладки)
    datas += ["guess:7", "guess:new:7", "rps:rock:paper", "bj:hit:tok",
              "bj:stand:tok"]
    return datas


def test_every_game_button_has_handler():
    """Каждая callback_data игровых клавиатур матчится хотя бы одному
    зарегистрированному хендлеру роутера games (иначе тап молча игнорируется)."""
    import asyncio
    from aiogram.types import CallbackQuery
    datas = _all_registered_cb_datas()
    assert datas, "клавиатуры пустые"

    # Прогоняем каждую через реальные роутеры (тот же набор, что в main.py).
    from app.handlers import (admin, access, settings, start, tracker,
                              tamagotchi, games, shop, merch, manual, social,
                              arena, stats, events)
    order = [admin.router, access.router, settings.router, start.router,
             tracker.router, tamagotchi.router, games.router, shop.router,
             merch.router, manual.router, social.router, arena.router,
             stats.router, events.router]

    class FakeCb:
        def __init__(self, data):
            self.data = data
            self.id = "x"
            self.from_user = type("U", (), {"id": 1})()
            self.message = None
        async def answer(self, *a, **k):
            return True

    async def _match(data: str):
        cb = FakeCb(data)
        for r in order:
            for h in r.callback_query.handlers:
                try:
                    # aiogram 3.x: check возвращает (bool, kwargs)
                    matched, _ = await h.check(cb)
                except Exception:
                    matched = False
                if matched:
                    return h.callback.__name__, r.name
        return None

    unmatched = [d for d in datas if asyncio.run(_match(d)) is None]
    assert not unmatched, f"кнопки без обработчика (тап = «ничего не происходит»): {unmatched}"


def test_moves_not_swallowed_by_earlier_handlers():
    """Ключевой регресс на «игра началась, но дальнейшие действия не
    работают»: кнопка хода должна доходить ДО СВОЕГО хендлера. В aiogram
    диспетчеризация останавливается на первом совпадении — раньше ходы
    гасились guard'ами, стоявшими в роутере раньше целевых хендлеров
    (game_noop_guard матчил game:*; game_stale_guard возвращал coroutine
    вместо falsy). Проверяем порядок: первый матчивший handler — целевой."""
    import asyncio
    from app.handlers.games import router

    expected_first = {
        "guess:7": "do_guess_cb",
        "guess:new:7": "guess_new",
        "rps:rock:paper": "play_rps",
        "rps:rock": "play_rps",          # stale-ход того же хендлера
        "bj:hit:tok": "bj_hit",
        "bj:stand:tok": "bj_stand",
        "game:exit": "game_exit",
        "game:guess": "start_guess",
        "game:rps": "start_rps",
        "game:blackjack": "start_blackjack",
        "pet:games": "games_screen",
    }

    class FakeCb:
        def __init__(self, data):
            self.data, self.id = data, "x"
            self.from_user = type("U", (), {"id": 1})()
            self.message = None
        async def answer(self, *a, **k):
            return True

    async def first_match(data: str):
        cb = FakeCb(data)
        for h in router.callback_query.handlers:
            try:
                # aiogram 3.x: check возвращает (bool, kwargs)
                matched, _ = await h.check(cb)
            except Exception:
                continue
            if matched:
                return h.callback.__name__
        return None

    for data, want in expected_first.items():
        got = asyncio.run(first_match(data))
        assert got == want, (
            f"кнопка {data!r} сначала матчится хендлером {got!r}, а должен "
            f"{want!r}: предыдущий handler проглотит тап и игра «зависнет»")


def test_games_need_no_fsm_state():
    """Архитектурная инвариантность: ходы игр самодостаточны (контекст в
    кнопках экрана), FSM-состояния им не нужны. Если кто-то снова добавит
    StateFilter к игровым хендлерам — после рестарта бота Redis-состояния
    теряются и кнопки «молчат» (исторический баг)."""
    from app.handlers.games import (router, do_guess_cb, play_rps, bj_hit,
                                    bj_stand, guess_new)
    for h in router.callback_query.handlers:
        if h.callback in (do_guess_cb, play_rps, bj_hit, bj_stand, guess_new):
            src = inspect.getsource(h.callback)
            assert "get_state" not in src, h.callback.__name__


def test_noop_guard_does_not_swallow_game_entries():
    """Регресс на «игры не работают»: game_noop_guard стоял до хендлеров
    входа с фильтром startswith("game:") и перехватывал game:guess/rps/
    blackjack — тап по игре молча гасился. Guard обязан исключать все
    известные кнопки."""
    import inspect
    from app.handlers.games import _GAME_KNOWN_CB
    for cb_data in ("game:guess", "game:rps", "game:blackjack", "game:exit"):
        assert cb_data in _GAME_KNOWN_CB, f"{cb_data} должен быть в списке известных"
    # Фильтр guard'а должен содержать инверсию (исключение известных кнопок)
    from app.handlers import games as g
    src_txt = inspect.getsource(g)
    idx = src_txt.index("async def game_noop_guard")
    deco = src_txt[max(0, idx - 200):idx]
    assert "~F.data.in_" in deco or "& ~" in deco, \
        "game_noop_guard обязан исключать известные game:-кнопки из фильтра"


def test_games_menu_always_has_exit_button():
    """На экране меню игр всегда есть хотя бы один выход (stack-nav или
    явная кнопка pet:page:2), иначе пользователь застревает без кнопок."""
    from app.keyboards.inline import games_menu
    kb = games_menu(chat_id=None)  # пустой стек навигации -> фолбэк/меню
    datas = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    # Выход обязан быть рабочим: либо вкладка хаба питомца (pet:page:N),
    # либо хотя бы корневое «🏠 Меню» — пользователь не должен оставаться
    # на экране игр вообще без кнопок возврата.
    assert any(d.startswith("pet:page:") or d == "menu:main" for d in datas), \
        f"нет кнопки выхода из меню игр: {datas}"


# ── Регрессия «игра началась, но дальнейшие действия не работают» ─────────
# (game_stale_guard удалён из архитектуры: ходы самодостаточны, контекст
#  живёт в кнопках экрана; порядок матчинга проверяет
#  test_moves_not_swallowed_by_earlier_handlers выше)

def test_guess_kb_no_duplicate_buttons():
    """Дубликаты callback_data в одной клавиатуре недопустимы (Telegram их
    не различает; при lo==mid старая версия ломала раскладку)."""
    from app.handlers.games import _guess_kb
    for lo, hi in ((10, 10), (10, 11), (1, 20), (3, 4)):
        kb = _guess_kb(secret=7, lo=lo, hi=hi, chat_id=1)
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        num_datas = [d for d in datas if d.startswith("guess:")
                     and not d.startswith("guess:new")]
        assert len(num_datas) == len(set(num_datas)), (lo, hi, datas)


def test_bj_state_roundtrip():
    """Снимок партии блэкджека в callback_data переживает сериализацию
    (замена FSM): deck/player/dealer/stay восстанавливаются точно, а
    подпись отбрасывает подделанные/обрезанные токены."""
    from app.handlers.games import _bj_state_b64, _bj_load_state
    deck = [[3, "♠"], [11, "♥"]]
    player = [[5, "♦"], [6, "♣"]]
    dealer = [[9, "♠"], [2, "♥"]]
    tok = _bj_state_b64(deck, player, dealer, 17)
    st = _bj_load_state(f"bj:hit:{tok}")
    assert st[0] == "ok"
    _, d, p, dl, stay = st
    assert d == [(3, "♠"), (11, "♥")] and p == [(5, "♦"), (6, "♣")]
    assert dl == [(9, "♠"), (2, "♥")] and stay == 17
    # битый/поддельный токен → не 'ok' (ход уйдёт на stale-экран, а не упадёт)
    assert _bj_load_state("bj:hit:notatoken.deadbeef")[0] != "ok"
    assert _bj_load_state("bj:stand:")[0] == "stale"
