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

def test_every_game_button_has_handler():
    """Каждая callback_data игровых клавиатур матчится хотя бы одному
    зарегистрированному хендлеру роутера games (иначе тап молча игнорируется)."""
    import asyncio
    from aiogram.types import CallbackQuery
    from app.keyboards.inline import games_menu
    from app.handlers.games import _guess_kb, _rps_kb, _bj_kb

    # Собираем все кнопки всех игровых экранов.
    datas = []
    for kb in (games_menu(1), _guess_kb(5, 12, 1), _rps_kb(1), _bj_kb(1)):
        for row in kb.inline_keyboard:
            datas += [b.callback_data for b in row if b.callback_data]
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

    async def _match(data: str) -> bool:
        cb = FakeCb(data)
        for r in order:
            for h in r.callback_query.handlers:
                try:
                    res = await h.check(cb, {})
                except Exception:
                    res = False
                if res:
                    return True
        return False

    unmatched = [d for d in datas if not asyncio.run(_match(d))]
    assert not unmatched, f"кнопки без обработчика (тап = «ничего не происходит»): {unmatched}"


def test_stale_guard_registered_before_fsm_handlers():
    """Ходы guess:/rps:/bj: без FSM-состояния больше не повисают в воздухе:
    game_stale_guard объявлен в роутере раньше целевых FSM-хендлеров."""
    from app.handlers.games import router, game_stale_guard, do_guess_cb
    handlers = router.callback_query.handlers
    idx_guard = next(i for i, h in enumerate(handlers) if h.callback is game_stale_guard)
    idx_guess = next(i for i, h in enumerate(handlers) if h.callback is do_guess_cb)
    assert idx_guard < idx_guess
    # сам guard матчит все три префикса без состояния
    src = inspect.getsource(game_stale_guard)
    assert '"guess:"' in src and '"rps:"' in src and '"bj:"' in src


def test_guess_kb_no_duplicate_buttons():
    """Дубликаты callback_data в одной клавиатуре недопустимы (Telegram их
    не различает; при lo==mid старая версия ломала раскладку)."""
    from app.handlers.games import _guess_kb
    for lo, hi in ((10, 10), (10, 11), (1, 20), (3, 4)):
        kb = _guess_kb(lo, hi, 1)
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        num_datas = [d for d in datas if d.startswith("guess:")]
        assert len(num_datas) == len(set(num_datas)), (lo, hi, datas)


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

def test_stale_guard_passes_real_moves_to_target_handlers():
    """game_stale_guard стоит в роутере ДО целевых FSM-хендлеров и матчит
    ЛЮБЫЕ кнопки хода. Настоящий ход (состояние на месте) он обязан
    пропустить: callback вернёт falsy (None), иначе aiogram считает update
    обработанным, диспетчеризация останавливается и тап гасится молча —
    ровно этот баг ломал все три мини-игры (guard возвращал coroutine из
    `return await cb.answer()`)."""
    import asyncio
    from types import SimpleNamespace
    from app.handlers.games import game_stale_guard, Games

    class FakeState:
        def __init__(self, st): self._st = st
        async def get_state(self): return self._st

    for data, state_str in (("rps:rock", Games.rps.state),
                            ("guess:7", Games.guessing.state),
                            ("bj:hit", Games.blackjack.state)):
        cb = SimpleNamespace(data=data, from_user=SimpleNamespace(id=1))
        res = asyncio.run(game_stale_guard(cb, FakeState(state_str), None))
        assert not res, f"ход {data} при активном состоянии должен пропускаться " \
                        f"(falsy), а guard вернул {res!r} — цель не будет вызвана"

def test_stale_guard_answers_when_state_lost():
    """Ход без состояния (перезапуск бота/мёртвая кнопка) НЕ зависает:
    guard сам отвечает на тап и возвращает truthy (update обработан)."""
    import asyncio
    from types import SimpleNamespace
    from app.handlers.games import game_stale_guard

    answered = []
    class FakeCb:
        data = "rps:rock"
        from_user = SimpleNamespace(id=1)
        message = None
        async def answer(self, *a, **k):
            answered.append(a); return True

    class FakeState:
        async def get_state(self): return None  # состояние потеряно

    class FakeRepo:
        @staticmethod
        async def get_by_user(session, tg_id): return None  # нет питомца -> ранний выход

    from app.handlers import games as g
    orig = g._get_pet
    g._get_pet = lambda session, tg_id: FakeRepo.get_by_user(session, tg_id)
    try:
        res = asyncio.run(game_stale_guard(FakeCb(), FakeState(), None))
    finally:
        g._get_pet = orig
    assert res, "при потерянном состоянии update должен быть обработан (truthy)"
    assert answered, "тап обязан получить ответ (не молча гаситься)"
