"""Регресс v1.3.4/v1.3.6: BotRuntime.start() не должен быть @staticmethod.

На проде live-режим падал с `BotRuntime.start() missing 1 required positional
argument: 'self'` — метод был помечен @staticmethod, но использовал self.
Тест проверяет корректность связки livelog -> runtime (мок внешних зависимостей)
и отсутствие аналогичных багов во всём пакете app.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import pathlib

import pytest


def test_start_is_instance_method():
    from app.console.runtime import BotRuntime

    assert not isinstance(
        inspect.getattr_static(BotRuntime, "start"), staticmethod
    ), "BotRuntime.start не должен быть @staticmethod (использует self)"
    sig = inspect.signature(BotRuntime.start)
    assert list(sig.parameters)[0] == "self"
    assert inspect.iscoroutinefunction(BotRuntime.start)


def test_stop_is_instance_method():
    from app.console.runtime import BotRuntime

    assert not isinstance(
        inspect.getattr_static(BotRuntime, "stop"), staticmethod
    )


def test_no_staticmethod_with_self_arg_in_app_package():
    """Ни один @staticmethod в app/ не должен принимать self/cls первым."""
    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        decs = [ast.unparse(d) for d in item.decorator_list]
                        args = [a.arg for a in item.args.args]
                        if "staticmethod" in decs and args and args[0] in ("self", "cls"):
                            offenders.append(f"{path.name}:{item.lineno} {node.name}.{item.name}")
    assert not offenders, f"@staticmethod c self/cls: {offenders}"


@pytest.mark.asyncio
async def test_livelog_calls_bound_runtime_start(monkeypatch, tmp_path):
    """amain() из livelog должен дойти до связанного runtime.start и корректно остановиться."""
    from app.console import livelog, runtime as rt_mod

    calls: list[str] = []

    async def fake_start(self) -> None:
        calls.append("start")
        self.state = "running"
        # имитация реального start(): поллинг крутится, пока его не отменят
        self._polling_task = asyncio.ensure_future(asyncio.Event().wait())

    async def fake_stop(self) -> None:
        calls.append("stop")
        if self._polling_task is not None:
            self._polling_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._polling_task
            self._polling_task = None
        self.state = "stopped"

    monkeypatch.setattr(rt_mod.BotRuntime, "start", fake_start)
    monkeypatch.setattr(rt_mod.BotRuntime, "stop", fake_stop)
    monkeypatch.chdir(tmp_path)  # logs/ от setup_live_logging попадёт во временную папку

    stop = asyncio.Event()

    async def fake_amain():
        await rt_mod.BotRuntime.start(rt_mod.runtime)   # ровно та же связка, что в livelog
        calls.append("after-start")
        await stop.wait()
        return 0

    task = asyncio.create_task(fake_amain())
    await asyncio.sleep(0.02)
    assert "start" in calls and "after-start" in calls, \
        "связанный BotRuntime.start() не выполнился (баг @staticmethod)"
    stop.set()
    code = await asyncio.wait_for(task, timeout=5)
    assert "start" in calls, "runtime.start не вызван (связка livelog->runtime сломана)"
    assert code == 0
