"""Совместимость с Redis/Memurai < 6.0 (исправление 'unknown command HELLO').

redis-py>=5 по умолчанию использует RESP3-протокол, который шлёт команду
HELLO при подключении. Memurai для Windows и старый Redis (<6.0) её не
знают — любой запрос к FSM падал с ResponseError: unknown command 'HELLO'.
Исправление v1.3.0: явный protocol=2 (RESP2) во всех точках подключения.
"""
from __future__ import annotations

import pytest

from app.main import _make_fsm_storage


def test_fsm_storage_forces_resp2_protocol() -> None:
    storage = _make_fsm_storage("redis://localhost:6379/0")
    pool = getattr(storage.redis, "connection_pool", storage.redis)
    assert pool.connection_kwargs.get("protocol") == 2, \
        "RedisStorage должен использовать протокол RESP2 (совместимость со старым Redis)"


def test_fsm_storage_falls_back_on_broken_url() -> None:
    from aiogram.fsm.storage.memory import MemoryStorage
    storage = _make_fsm_storage("not-a-valid-url")
    assert isinstance(storage, MemoryStorage)


@pytest.mark.asyncio
async def test_utils_redis_client_uses_resp2(monkeypatch) -> None:
    """init_redis() тоже обязан ставить protocol=2 (кулдауны/локи)."""
    from redis.asyncio import Redis
    r = Redis.from_url("redis://localhost:6379/0", decode_responses=True, protocol=2)
    assert r.connection_pool.connection_kwargs.get("protocol") == 2
    await r.aclose()
