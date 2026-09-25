"""Регресс на проде-ошибку: DataError('ex must be datetime.timedelta or int').

redis-py>=5 принимает в SET/GETSET только int/timedelta. Дробные TTL
(THROTTLE_SEC=1.5) и любые строки из .env должны нормализоваться к int.
Также регресс на 'ConnectionPool' object has no attribute 'get' —
RedisStorage обязан получать экземпляр Redis, а не пул.
"""
from __future__ import annotations

import pytest

from app.utils.redis import _mem_store, _norm_ttl, redis_client, set_cooldown


def test_norm_ttl_accepts_float_str_int_none():
    assert _norm_ttl(10) == 10            # int
    assert _norm_ttl(1.5) == 2            # float -> ceil-ish (int()+max1)
    assert _norm_ttl("10") == 10          # str из env
    assert _norm_ttl("abc") == 1          # мусор -> минимум 1
    assert _norm_ttl(None) == 1           # None -> минимум 1
    assert _norm_ttl(0) == 1              # ноль недопустим для EX
    assert _norm_ttl(-5) == 1             # отрицательный clamp
    assert isinstance(_norm_ttl(3.7), int)


@pytest.mark.asyncio
async def test_set_cooldown_with_float_ttl_no_redis(monkeypatch):
    """Без Redis путь mem-store не должен падать на дробном TTL."""
    monkeypatch.setattr("app.utils.redis.redis_client", None)
    _mem_store.clear()
    assert await set_cooldown("t:float", 1.5) is True
    assert await set_cooldown("t:float", 1.5) is False   # кулдаун сработал
    assert await set_cooldown("t:str", "10") is True
    _mem_store.clear()


@pytest.mark.asyncio
async def test_set_cooldown_passes_int_to_redis(monkeypatch):
    """С рабочим клиентом ex обязан уходить целым числом (регресс DataError)."""
    captured: dict = {}

    class FakeRedis:
        async def ping(self):
            return True

        async def set(self, key, value, nx=False, ex=None):
            captured["ex"] = ex
            if not isinstance(ex, int):
                raise TypeError(f"ex must be int, got {type(ex)}")
            return True

    monkeypatch.setattr("app.utils.redis.redis_client", FakeRedis())
    assert await set_cooldown("cb:1:x", 1.5) is True
    assert captured["ex"] == 2 and isinstance(captured["ex"], int)


def test_fsm_storage_uses_redis_instance_not_pool():
    """RedisStorage.get_state вызывает self.redis.get(...) — пул так не умеет."""
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.fsm.storage.redis import RedisStorage

    from app.main import _make_fsm_storage

    storage = _make_fsm_storage("redis://127.0.0.1:6399/0")  # порта нет — но конструктор ленив
    assert isinstance(storage, (RedisStorage, MemoryStorage))
    if isinstance(storage, RedisStorage):
        # ключевая проверка: это Redis, а не ConnectionPool
        assert hasattr(storage.redis, "get"), "RedisStorage должен получать Redis, не pool"
