from __future__ import annotations

import time
from typing import Any

from loguru import logger
from redis.asyncio import Redis

from app.config import get_settings

_settings = get_settings()

redis_client: Redis | None = None

_mem_store: dict[str, float] = {}

def _norm_ttl(ttl_sec: Any) -> int:
    import math
    try:
        value = math.ceil(float(ttl_sec))
    except (TypeError, ValueError):
        value = 1
    return max(value, 1)

def init_redis() -> Redis:
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
    if redis_client is None:
        return None
    try:
        await redis_client.ping()
        return redis_client
    except Exception as exc:
        key = type(exc).__name__
        if key not in _warned_errors:
            _warned_errors.add(key)
            logger.warning(
                f"Redis недоступен ({key}: {exc}) — переключаюсь на in-memory "
                f"кулдауны/кэш (сбрасываются при рестарте)"
            )
        return None

async def set_cooldown(key: str, ttl_sec: Any) -> bool:
    r = await _try_redis()
    if r is not None:
        try:
            return bool(await r.set(f"cd:{key}", "1", nx=True, ex=_norm_ttl(ttl_sec)))
        except TypeError as exc:
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
    r = await _try_redis()
    if r is not None:
        ttl = await r.ttl(f"cd:{key}")
        return max(ttl, 0)
    exp = _mem_store.get(f"cd:{key}")
    if exp is None:
        return 0
    return max(int(exp - time.monotonic()), 0)

async def acquire_lock(name: str, ttl_sec: int = 60) -> bool:
    r = await _try_redis()
    if r is not None:
        try:
            return bool(await r.set(f"lock:{name}", "1", nx=True, ex=_norm_ttl(ttl_sec)))
        except TypeError:
            key_full = f"lock:{name}"
            ok = await r.set_nx_ex(key_full, "1", _norm_ttl(ttl_sec)) \
                if hasattr(r, "set_nx_ex") else await r.setnx(key_full, "1")
            if ok:
                await r.expire(key_full, _norm_ttl(ttl_sec))
                return True
            return False
    return True

async def release_lock(name: str) -> None:
    r = await _try_redis()
    if r is not None:
        await r.delete(f"lock:{name}")
    else:
        _mem_store.pop(f"lock:{name}", None)


async def renew_lock(name: str, ttl_sec: int) -> bool:
    """Продлить/взять лок (idempotent): используется для самолечения cron-джоб,
    чей TTL больше периода запуска."""
    r = await _try_redis()
    if r is not None:
        try:
            return bool(await r.set(f"lock:{name}", "1", xx=False, ex=_norm_ttl(ttl_sec)))
        except TypeError:
            key_full = f"lock:{name}"
            await r.set(key_full, "1")
            await r.expire(key_full, _norm_ttl(ttl_sec))
            return True
    _mem_store[f"lock:{name}"] = time.monotonic() + float(_norm_ttl(ttl_sec))
    return True

async def remember_for(name: str, ttl_sec: int) -> bool:
    r = await _try_redis()
    if r is not None:
        try:
            ok = await r.set(f"every:{name}", "1", nx=True, ex=_norm_ttl(ttl_sec))
            return bool(ok)
        except Exception as exc:
            logger.debug("remember_for redis failed ({}): mem mode", exc)
    k = f"every:{name}"
    now = time.monotonic()
    exp = _mem_store.get(k)
    if isinstance(exp, float) and exp > now:
        return False
    _mem_store[k] = now + float(_norm_ttl(ttl_sec))
    return True

async def mem_cached_set(key: str, value: str, ttl_sec: int = 3600) -> str | None:
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


async def incr_counter(key: str, amount: int = 1, ttl: int | None = None) -> int:
    """Атомарно увеличивает счётчик; возвращает новое значение.

    TTL продлевается только при создании ключа (чтобы дневные окна
    не «сдвигались» с каждым инкрементом).
    """
    r = await _try_redis()
    if r is not None:
        try:
            ck = f"cnt:{key}"
            new = int(await r.incrby(ck, int(amount)))
            if new == int(amount) and ttl:
                await r.expire(ck, _norm_ttl(ttl))
            elif ttl:
                cur = await r.ttl(ck)
                if cur is None or int(cur) < 0:
                    await r.expire(ck, _norm_ttl(ttl))
            return new
        except Exception as exc:
            logger.debug("incr_counter redis failed ({}): mem mode", exc)
    ck = f"cnt:{key}"
    now = time.monotonic()
    exp = _mem_store.get(ck + ":exp")
    if isinstance(exp, float) and exp <= now:
        _mem_store.pop(ck, None)
        _mem_store.pop(ck + ":exp", None)
    val = int(_mem_store.get(ck, 0) or 0) + int(amount)
    _mem_store[ck] = val
    if ttl and (ck + ":exp") not in _mem_store:
        _mem_store[ck + ":exp"] = now + float(_norm_ttl(ttl))
    return val


async def get_counter(key: str) -> int:
    r = await _try_redis()
    if r is not None:
        try:
            raw = await r.get(f"cnt:{key}")
            if raw is None:
                return 0
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            return int(raw)
        except Exception as exc:
            logger.debug("get_counter redis failed ({}): mem mode", exc)
    ck = f"cnt:{key}"
    now = time.monotonic()
    exp = _mem_store.get(ck + ":exp")
    if isinstance(exp, float) and exp <= now:
        return 0
    try:
        return int(_mem_store.get(ck, 0) or 0)
    except (TypeError, ValueError):
        return 0
