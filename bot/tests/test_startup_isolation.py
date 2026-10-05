"""Регресс: отказ одной фазы on_startup не срывает остальные (проблема №9).

Раньше on_startup был линейным: исключение в любой миграции (например, в
_migrate_channel_subscribers_v20 при битой таблице) прерывало весь запуск —
команды бота не регистрировались, справочники не засидировались. Теперь
каждая фаза изолирована try/except с логированием, а список неудачных фаз
выводится одним предупреждением.
"""
import os
import sys

sys.path.insert(0, os.path.dirname("app"))

import app.main as m


class FakeBot:
    """Заглушка Bot: считает вызовы, чтобы подтвердить достижение финальной фазы."""

    def __init__(self):
        self.calls = 0

    async def delete_my_commands(self, scope=None):
        self.calls += 1

    async def set_my_commands(self, commands, scope=None):
        self.calls += 1


def _patch_steps(monkeypatch, failing_names):
    """Подменяет тела фаз; падающие бросают RuntimeError, остальные — no-op.

    on_startup читает функции из модуля в момент вызова (лямбды ленивы),
    поэтому monkeypatch атрибутов m действует на реальный on_startup —
    сам он не патчится, тест проверяет именно его изоляцию фаз.
    Финальная фаза (_sync_bot_commands) НЕ подменена: она выполняется
    реально на FakeBot, чей счётчик подтверждает её достижение.
    """
    executed: list[str] = []

    def make(name):
        async def run(*args, **kwargs):
            if name in failing_names:
                raise RuntimeError(f"boom:{name}")
            executed.append(name)
        return run

    monkeypatch.setattr(m, "_startup_schema_phase", make("schema"))
    monkeypatch.setattr(m, "ensure_events_table", make("events"))
    monkeypatch.setattr(m, "_migrate_channel_subscribers_v20", make("subs"))
    monkeypatch.setattr(m, "_backfill_subscriber_chats_v202", make("backfill"))
    monkeypatch.setattr(m, "_startup_seed_phase", make("seed"))
    return executed


async def test_broken_migration_does_not_block_other_phases(monkeypatch):
    """Упавшая миграция подписчиков не мешает сидам и регистрации команд."""
    executed = _patch_steps(monkeypatch, failing_names={"subs"})
    bot = FakeBot()

    await m.on_startup(bot)  # не должно бросить

    assert "subs" not in executed
    # Все остальные фазы выполнены, включая финальную (команды бота).
    assert set(executed) == {"schema", "events", "backfill", "seed"}
    assert bot.calls > 0, "синхронизация команд не должна зависеть от миграций"


async def test_all_phases_ok_when_no_failures(monkeypatch):
    executed = _patch_steps(monkeypatch, failing_names=set())
    bot = FakeBot()

    await m.on_startup(bot)

    assert len(executed) == 5
    assert bot.calls > 0


async def test_multiple_failures_are_all_logged_and_survived(monkeypatch):
    """Несколько упавших фаз: остальные всё равно выполняются, падения видны в логе.

    Логирует loguru (не стандартный logging/caplog), поэтому перехват
    ошибок делается через временный sink loguru.
    """
    import io

    from loguru import logger as _logger

    executed = _patch_steps(monkeypatch, failing_names={"schema", "seed"})
    bot = FakeBot()

    sink = io.StringIO()
    handler_id = _logger.add(sink, level="WARNING", enqueue=False)
    try:
        await m.on_startup(bot)
    finally:
        _logger.remove(handler_id)

    assert set(executed) == {"events", "backfill", "subs"}
    out = sink.getvalue()
    assert "создание схемы" in out and "boom:schema" in out
    assert "посев справочников" in out and "boom:seed" in out
    assert "запуск продолжен с 2 неудачной" in out
    assert bot.calls > 0
