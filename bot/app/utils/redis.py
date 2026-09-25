"""Redis: кулдауны, rate-limit, FSM-стейты, распределённые локи.

Если Redis недоступен (или не установлен) — graceful fallback на
in-memory словарь: бот работает полностью, но кулдауны/FSM сбрасываются
при рестарте процесса. Для Windows Server Redis опционален (Memurai).

Совместимость: клиент redis-py>=5 по умолчанию использует RESP3-протокол
(команда HELLO при подключении), которую не понимают Redis 2.x/3.x и
старые сборки Memurai (<6.0). Поэтому явно запрашиваем protocol=2 —
работает с любым сервером.
"""
from __future__ import annotations

import time
from typing import Any

from redis.asyncio import Redis

from app.config import get_settings

_settings = get_settings()

redis_client: Redis | None = None

# Fallback для dev без Redis
_mem_store: dict[str, float] = {}


def init_redis() -> Redis:
    """Создаёт пул клиентов Redis (RESP2 — совместим со старыми серверами)."""
    global redis_client
    redis_client = Redis.from_url(
        _settings.redis_url, decode_responses=True, protocol=2,
        socket_connect_timeout=2, socket_timeout=2,
    )
    return redis_client


async def close_redis() -> None:
    global redis_client
    if redis_client is not None:
        await redis_client.aclose()
        redis_client = None


_ping_cache: dict[str, tuple[float, bool]] = {}


async def _try_redis() -> Any:
    """Возвращает рабочий redis-клиент или None (при недоступности).

    PING кэшируется на 5 секунд: иначе каждый cooldown-check давал бы
    лишний roundtrip, а при недоступном сервере — тормозил обработку.
    Важно: здесь ловятся И ResponseError (старые серверы без RESP3 и т.п.)
    — тогда клиент не считается рабочим и включается in-memory fallback.
    """
    import time as _t
    now = _t.monotonic()
    cached = _ping_cache.get("ok")
    if cached and cached[0] > now:
        return redis_client if cached[1] else None
    result = False
    if redis_client is not None:
        try:
            await redis_client.ping()
            result = True
        except Exception:
            result = False
    _ping_cache["ok"] = (now + 5.0, result)
    return redis_client if result else None


async def set_cooldown(key: str, ttl_sec: int) -> bool:
    """Ставит кулдаун. Возвращает True, если кулдаун новый (можно засчитывать)."""
    r = await _try_redis()
    if r is not None:
        # SET NX EX — атомарно: False, если ключ уже есть
        return bool(await r.set(f"cd:{key}", "1", nx=True, ex=ttl_sec))
    now = time.monotonic()
    exp = _mem_store.get(f"cd:{key}")
    if exp is not None and exp > now:
        return False
    _mem_store[f"cd:{key}"] = now + ttl_sec
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
        return bool(await r.set(f"lock:{name}", "1", nx=True, ex=ttl_sec))
    return True  # без Redis один инстанс — локи не нужны


async def release_lock(name: str) -> None:
    r = await _try_redis()
    if r is not None:
        await r.delete(f"lock:{name}")


# ---------------------------------------------------------------------------
# Простое in-memory/Redis кэширование строк (версии карточек и т.п.)
# ---------------------------------------------------------------------------
async def mem_cached_set(key: str, value: str, ttl_sec: int = 3600) -> str | None:
    """Ставит значение, возвращает ПРЕДЫДУЩЕЕ (или None). Без Redis — mem-store."""
    r = await _try_redis()
    if r is not None:
        prev = await r.getset(f"cache:{key}", value, ex=ttl_sec)
        return prev
    k = f"cache:{key}"
    prev = _mem_store.get(k)
    _mem_store[k] = value
    return prev if isinstance(prev, str) else None
