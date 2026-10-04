"""Регрессия на баг: бот ставил реакцию (🎉/😿) под сообщением-итогом
мини-игры («Ты победил!»), а в магазине — 🪙. Реакции на сообщения
пользователя бот ставить НЕ должен: фидбэк действий = тост (cb.answer)
и текст итогового сообщения.

Проверяем два уровня:
 1) статически — в app/ нет вызовов SetMessageReaction / react_to_message;
 2) поведенчески — apply_effect с toast_override отвечает только cb.answer
    и не трогает никакие методы бота (в т.ч. реакции).
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest


APP_DIR = Path(__file__).resolve().parents[1] / "app"

FORBIDDEN = {"SetMessageReaction", "react_to_message"}


def test_no_reaction_calls_in_app():
    offenders: list[str] = []
    for py in APP_DIR.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in FORBIDDEN:
                offenders.append(f"{py.relative_to(APP_DIR)}:{node.lineno}:{node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN:
                offenders.append(f"{py.relative_to(APP_DIR)}:{node.lineno}:{node.attr}")
    assert not offenders, "бот не должен ставить реакции: " + "; ".join(offenders)


class _Recorder:
    def __init__(self):
        self.calls: list[tuple] = []

    async def answer(self, text=None, **kw):
        self.calls.append(("answer", text))


def test_apply_effect_only_answers_and_sets_nothing(monkeypatch):
    from app.utils.fx import apply_effect

    cb = _Recorder()
    # Старые вызовы с react_target/message не должны ломать API и не должны
    # порождать ничего, кроме cb.answer.
    asyncio.run(apply_effect(cb, "win", toast_override="🏆 Победа!",
                             react_target=object(), message=object()))
    assert cb.calls == [("answer", "🏆 Победа!")]

    cb2 = _Recorder()
    asyncio.run(apply_effect(cb2, "play"))
    assert cb2.calls == [("answer", "🎮 Игра состоялась!")]

    cb3 = _Recorder()
    asyncio.run(apply_effect(cb3, "unknown-action"))
    assert cb3.calls == [("answer", None)]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
