"""Регрессия Issue #10: cron-джобы планировщика не должны падать молча.

_safe(fn) обязан:
  1) пропускать успешный вызов насквозь (сохраняя результат);
  2) перехватывать любые исключения джоба, логируя traceback, но НЕ глотая
     asyncio.CancelledError (иначе планировщик/остановка приложения подвиснет);
  3) сохранять имя функции (functools.wraps) — APScheduler и тесты опираются
     на __name__ при регистрации задач;
  4) оборачивать ВСЕ add_job в build_scheduler (ни одна задача не должна
     оставаться без защиты).
"""
import asyncio
import inspect

import pytest

from app.tasks import scheduler as sched_mod


def test_safe_passes_through_result_and_kwargs():
    async def job(a, b=None):
        return (a, b)

    wrapped = sched_mod._safe(job)
    assert asyncio.run(wrapped(1, b=2)) == (1, 2)
    # functools.wraps сохраняет метаданные оригинала
    assert wrapped.__name__ == "job"
    assert inspect.iscoroutinefunction(wrapped)


def test_safe_swallows_exception(caplog):
    async def bad_job(bot):
        raise RuntimeError("boom")

    result = asyncio.run(sched_mod._safe(bad_job)(object()))
    assert result is None  # ошибка не всплывает наружу — джоб живёт дальше


def test_safe_logs_via_logger_exception(monkeypatch):
    logged = {}

    class FakeLogger:
        def exception(self, msg, *args):
            logged["msg"] = msg.format(*args)
            logged["exc"] = True

    monkeypatch.setattr(sched_mod, "logger", FakeLogger())

    async def bad_job():
        raise ValueError("kaboom")

    asyncio.run(sched_mod._safe(bad_job)())
    assert logged.get("exc") is True
    assert "bad_job" in logged.get("msg", "")


def test_safe_propagates_cancelled_error():
    async def slow_job():
        await asyncio.sleep(5)

    wrapped = sched_mod._safe(slow_job)

    async def run_and_cancel():
        task = asyncio.ensure_future(wrapped())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_and_cancel())


def test_all_jobs_registered_wrapped_in_safe():
    src = inspect.getsource(sched_mod.build_scheduler)
    n_add = src.count("add_job(")
    n_safe = src.count("add_job(_safe(")
    assert n_add > 0, "ожидаем непустой список задач"
    assert n_add == n_safe, (
        f"{n_add - n_safe} задач(и) зарегистрированы без _safe-обёртки — "
        "их падения будут молчать в логах APScheduler")
