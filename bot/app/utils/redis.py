"""Redis: кулдауны, rate-limit, FSM-стейты, распределённые локи.

Если Redis недоступен — graceful fallback на in-memory словарь
(для разработки; в проде Redis обязателен).

ВАЖНО про TTL: redis-py>=5 принимает только int или datetime.timedelta
(строки вызывают DataError "ex must be datetime.timedelta or int").
Все вызывающие места нормализуют ttl через _norm_ttl().
"""
from __future__ import annotations

import time
from typing import Any

from loguru import logger
from redis.asyncio import Redis

from app.config import get_settings

_settings = get_settings()

redis_client: Redis | None = None

# Fallback для dev без Redis
_mem_store: dict[str, float] = {}


def _norm_ttl(ttl_sec: Any) -> int:
    """Нормализует TTL к целому числу секунд (int).

    Совместимо с redis-py 5+/8+, где ex должен быть int/timedelta.
    Дробные значения (например, THROTTLE_SEC = 1.5) округляются вверх,
    минимум — 1 секунда.
    """
    import math
    try:
        value = math.ceil(float(ttl_sec))
    except (TypeError, ValueError):
        value = 1
    return max(value, 1)


def init_redis() -> Redis:
    """Создаёт Redis-клиент.

    protocol=2 (RESP2) — обязательно для совместимости со старыми
    серверами Redis/Memurai (< 6.0), которые не знают команду HELLO.
    Таймауты защищают от зависания на недоступном сервере.
    """
    global redis_client
    redis_client = Redis.from_url(
        _settings.redis_url,
        decode_responses=True,
        protocol=2,
        socket_timeout=getattr(_settings, "redis_socket_timeout", 5),
        socket_connect_timeout=getattr(_settings, "redis_socket_timeout", 5),
    )
    return redis_client


async def close_redis() -> None:
    global redis_client
    if redis_client is not None:
        await redis_client.aclose()
        redis_client = None


_warned_errors: set[str] = set()


async def _try_redis() -> Any:
    """Возвращает рабочий redis-клиент или None (при недоступности).

    Ошибки команд (например, ResponseError от старого сервера) логируются
    один раз на тип ошибки, чтобы не спамить в лог при каждом апдейте.
    """
    if redis_client is None:
        return None
    try:
        await redis_client.ping()
        return redis_client
    except Exception as exc:  # noqa: BLE001 — fallback по замыслу
        key = type(exc).__name__
        if key not in _warned_errors:
            _warned_errors.add(key)
            logger.warning(
                f"Redis недоступен ({key}: {exc}) — переключаюсь на in-memory "
                f"кулдауны/кэш (сбрасываются при рестарте)"
            )
        return None


async def set_cooldown(key: str, ttl_sec: Any) -> bool:
    """Ставит кулдаун. Возвращает True, если кулдаун новый (можно засчитывать)."""
    r = await _try_redis()
    if r is not None:
        try:
            # SET NX EX — атомарно: False, если ключ уже есть.
            # ex обязан быть int (redis-py>=5), поэтому _norm_ttl.
            return bool(await r.set(f"cd:{key}", "1", nx=True, ex=_norm_ttl(ttl_sec)))
        except TypeError as exc:  # redis-py>=6 убрал kwarg ex (NX_EX_DEPRECATED)
            key_full = f"cd:{key}"
            ok = await r.set_nx_ex(key_full, "1", _norm_ttl(ttl_sec)) \
                if hasattr(r, "set_nx_ex") else await r.setnx(key_full, "1")
            if ok:
                await r.expire(key_full, _norm_ttl(ttl_sec))
                return True
            logger.debug(f"set_cooldown compat-path: {exc}")
            return False
    now = time.monotonic()
    exp = _mem_store.get(f"cd:{key}")
    if exp is not None and exp > now:
        return False
    _mem_store[f"cd:{key}"] = now + float(_norm_ttl(ttl_sec))
    return True


async def get_cooldown_ttl(key: str) -> int:
    """Сколько секунд осталось до конца кулдауна (0 — кулдауна нет)."""
    r = await _try_redis()
    if r is not None:
        ttl = await r.ttl(f"cd:{key}")
        return max(ttl, 0)
    exp = _mem_store.get(f"cd:{key}")
    if exp is None:
        return 0
    return max(int(exp - time.monotonic()), 0)


async def acquire_lock(name: str, ttl_sec: int = 60) -> bool:
    """Простой Redis-lock для задач планировщика (масштабирование на N воркеров)."""
    r = await _try_redis()
    if r is not None:
        try:
            return bool(await r.set(f"lock:{name}", "1", nx=True, ex=_norm_ttl(ttl_sec)))
        except TypeError:  # redis-py>=6: отдельная ветка NX+EX
            key_full = f"lock:{name}"
            ok = await r.set_nx_ex(key_full, "1", _norm_ttl(ttl_sec)) \
                if hasattr(r, "set_nx_ex") else await r.setnx(key_full, "1")
            if ok:
                await r.expire(key_full, _norm_ttl(ttl_sec))
                return True
            return False
    return True  # без Redis один инстанс — локи не нужны


async def release_lock(name: str) -> None:
    r = await _try_redis()
    if r is not None:
        await r.delete(f"lock:{name}")


# ---------------------------------------------------------------------------
# Простое in-memory/Redis кэширование строк (версии карточек и т.п.)
# ---------------------------------------------------------------------------
async def mem_cached_set(key: str, value: str, ttl_sec: int = 3600) -> str | None:
    """Ставит значение, возвращает ПРЕДЫДУЩЕЕ (или None). Без Redis — mem-store.

    Примечание: GETSET в redis-py не принимает kwarg ``ex`` (TTL задаётся
    отдельной командой EXPIRE) — это исправлено после TypeError на проде.
    """
    r = await _try_redis()
    if r is not None:
        redis_key = f"cache:{key}"
        prev_raw = await r.getset(redis_key, value)
        await r.expire(redis_key, _norm_ttl(ttl_sec))
        if isinstance(prev_raw, bytes):
            prev_raw = prev_raw.decode("utf-8", "replace")
        return prev_raw
    k = f"cache:{key}"
    prev = _mem_store.get(k)
    _mem_store[k] = value
    return prev if isinstance(prev, str) else None
