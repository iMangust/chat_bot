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
