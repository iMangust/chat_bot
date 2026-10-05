from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

from loguru import logger
from redis.asyncio import Redis

from app.config import get_settings

_settings = get_settings()

redis_client: Redis | None = None

# In-memory fallback storage with LRU eviction to prevent unbounded growth.
# Format: key -> expiry_time (monotonic clock).
# When Redis is unavailable, all cooldowns/locks/counters live here and are
# lost on restart (acceptable for dev/single-instance; production should have Redis).
_mem_store: OrderedDict[str, float] = OrderedDict()
_MEM_MAX_KEYS = 8192  # Cap to prevent memory leak during long uptimes with dead Redis

# Circuit breaker state: avoid hammering a dead Redis with ping() on every operation.
_redis_available: bool = True       # Assume available until first failure
_redis_last_check: float = 0.0      # Monotonic timestamp of last health check
_redis_backoff_sec: float = 30.0    # Recheck after this many seconds when unavailable
# Cooldowns written to the in-memory fallback while Redis was down are NOT
# mirrored back to Redis on reconnect — without this quarantine a just-recovered
# Redis would answer "no cooldown" and let duplicate messages/callbacks through.
_mem_quarantine_until: float = 0.0  # Monotonic deadline: prefer mem data over Redis

def _norm_ttl(ttl_sec: Any) -> int:
    import math
    try:
        value = math.ceil(float(ttl_sec))
    except (TypeError, ValueError):
        value = 1
    return max(value, 1)

def _mem_put(key: str, value: float) -> None:
    """Store in LRU cache with automatic eviction."""
    global _mem_quarantine_until
    _mem_store[key] = value
    _mem_store.move_to_end(key)
    while len(_mem_store) > _MEM_MAX_KEYS:
        _mem_store.popitem(last=False)
    # Quarantine: mem now holds cooldown/lock state Redis knows nothing about.
    # Keep serving from mem until the longest live deadline expires, so a
    # mid-outage write can't be "forgotten" right after Redis comes back.
    _mem_quarantine_until = max(_mem_quarantine_until, float(value))

def _mem_get(key: str) -> float | None:
    """Retrieve from LRU cache, updating access order."""
    val = _mem_store.get(key)
    if val is not None:
        _mem_store.move_to_end(key)
    return val

def _prefer_mem() -> bool:
    """True while mem fallback data must win over (possibly recovered) Redis."""
    return time.monotonic() < _mem_quarantine_until

def init_redis() -> Redis:
    global redis_client, _redis_available, _redis_last_check
    redis_client = Redis.from_url(
        _settings.redis_url,
        decode_responses=True,
        protocol=2,
        socket_timeout=getattr(_settings, "redis_socket_timeout", 5),
        socket_connect_timeout=getattr(_settings, "redis_socket_timeout", 5),
    )
    _redis_available = True
    _redis_last_check = 0.0
    return redis_client

async def close_redis() -> None:
    global redis_client
    if redis_client is not None:
        await redis_client.aclose()
        redis_client = None

_warned_errors: set[str] = set()

async def _try_redis() -> Any:
    """Check Redis availability with circuit breaker pattern.

    Returns Redis client if healthy, None otherwise. Uses exponential backoff
    to avoid hammering a dead server: once unavailable, we only recheck every
    _redis_backoff_sec seconds. This prevents DDoSing a struggling Redis with
    ping() on every message/cooldown check.
    """
    global _redis_available, _redis_last_check

    if redis_client is None:
        return None

    now = time.monotonic()

    # If marked unavailable, wait for backoff period before retrying
    if not _redis_available and now - _redis_last_check < _redis_backoff_sec:
        return None
        # Backoff expired, will attempt reconnect below

    # Health check (either initial or after backoff)
    try:
        await redis_client.ping()
        _redis_available = True
        _redis_last_check = now
        return redis_client
    except Exception as exc:
        was_available = _redis_available
        _redis_available = False
        _redis_last_check = now

        # Log transition to unavailable state (once per error type)
        if was_available:
            key = type(exc).__name__
            if key not in _warned_errors:
                _warned_errors.add(key)
                logger.warning(
                    f"Redis недоступен ({key}: {exc}) — переключаюсь на in-memory "
                    f"кулдауны/кэш (сбрасываются при рестарте). Повторная проверка "
                    f"через {_redis_backoff_sec:.0f} сек."
                )
        return None

async def set_cooldown(key: str, ttl_sec: Any) -> bool:
    # During post-outage quarantine mem data wins (Redis may not know about
    # cooldowns written while it was down).
    r = None if _prefer_mem() else await _try_redis()
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
        except Exception as exc:
            # Redis went down mid-operation (e.g. connection lost after ping):
            # fall through to in-memory instead of crashing the caller.
            logger.debug(f"set_cooldown redis failed ({type(exc).__name__}): mem mode")
    now = time.monotonic()
    exp = _mem_get(f"cd:{key}")
    if exp is not None and exp > now:
        return False
    _mem_put(f"cd:{key}", now + float(_norm_ttl(ttl_sec)))
    return True

async def get_cooldown_ttl(key: str) -> int:
    now = time.monotonic()
    if _prefer_mem():
        exp = _mem_store.get(f"cd:{key}")
        if isinstance(exp, float) and exp > now:
            return max(int(exp - now), 0)
    r = await _try_redis()
    if r is not None:
        try:
            ttl = await r.ttl(f"cd:{key}")
            return max(ttl, 0)
        except Exception as exc:
            logger.debug(f"get_cooldown_ttl redis failed ({type(exc).__name__}): mem mode")
    exp = _mem_get(f"cd:{key}")
    if exp is None:
        return 0
    return max(int(exp - time.monotonic()), 0)

async def acquire_lock(name: str, ttl_sec: int = 60) -> bool:
    r = None if _prefer_mem() else await _try_redis()
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
        except Exception as exc:
            # Redis died mid-op: fall through to in-memory lock below.
            logger.debug(f"acquire_lock redis failed ({type(exc).__name__}): mem mode")
    # In-memory fallback: real expiry check instead of unconditional True.
    # NOTE: single-process only — with several bot instances and dead Redis
    # each instance takes its own lock (same tradeoff as all mem fallbacks).
    now = time.monotonic()
    lk = f"lock:{name}"
    exp = _mem_get(lk)
    if exp is not None and exp > now:
        return False
    _mem_put(lk, now + float(_norm_ttl(ttl_sec)))
    return True

async def release_lock(name: str) -> None:
    r = await _try_redis()
    if r is not None:
        try:
            await r.delete(f"lock:{name}")
        except Exception as exc:
            logger.debug(f"release_lock redis failed ({type(exc).__name__}): mem mode")
    _mem_store.pop(f"lock:{name}", None)


async def renew_lock(name: str, ttl_sec: int) -> bool:
    """Продлить/взять лок (idempotent): используется для самолечения cron-джоб,
    чей TTL больше периода запуска."""
    r = None if _prefer_mem() else await _try_redis()
    if r is not None:
        try:
            return bool(await r.set(f"lock:{name}", "1", xx=False, ex=_norm_ttl(ttl_sec)))
        except TypeError:
            try:
                key_full = f"lock:{name}"
                await r.set(key_full, "1")
                await r.expire(key_full, _norm_ttl(ttl_sec))
                return True
            except Exception as exc:
                logger.debug(f"renew_lock redis failed ({type(exc).__name__}): mem mode")
        except Exception as exc:
            logger.debug(f"renew_lock redis failed ({type(exc).__name__}): mem mode")
    _mem_put(f"lock:{name}", time.monotonic() + float(_norm_ttl(ttl_sec)))
    return True

async def remember_for(name: str, ttl_sec: int) -> bool:
    r = None if _prefer_mem() else await _try_redis()
    if r is not None:
        try:
            ok = await r.set(f"every:{name}", "1", nx=True, ex=_norm_ttl(ttl_sec))
            return bool(ok)
        except Exception as exc:
            logger.debug(f"remember_for redis failed ({type(exc).__name__}): mem mode")
    k = f"every:{name}"
    now = time.monotonic()
    exp = _mem_get(k)
    if isinstance(exp, float) and exp > now:
        return False
    _mem_put(k, now + float(_norm_ttl(ttl_sec)))
    return True


# Sentinel distinguishes "key absent" from "stored value None" in the mem
# cache fallback (values are strings, so None can never collide with one).
_MISSING = object()

async def mem_cached_set(key: str, value: str, ttl_sec: int = 3600) -> str | None:
    # Cache values written to mem during an outage aren't mirrored back to
    # Redis; keep serving from mem until their TTLs elapse (see quarantine).
    # NOTE: _mem_put is intentionally NOT used here — cache entries are strings,
    # not float deadlines, and must not extend the cooldown quarantine window.
    r = None if _prefer_mem() else await _try_redis()
    if r is not None:
        try:
            redis_key = f"cache:{key}"
            prev_raw = await r.getset(redis_key, value)
            await r.expire(redis_key, _norm_ttl(ttl_sec))
            if isinstance(prev_raw, bytes):
                prev_raw = prev_raw.decode("utf-8", "replace")
            return prev_raw
        except Exception as exc:
            # Redis died mid-op: fall through to mem cache instead of crashing.
            logger.debug(f"mem_cached_set redis failed ({type(exc).__name__}): mem mode")
    k = f"cache:{key}"
    now = time.monotonic()
    exp = _mem_store.get(k + ":exp")
    # Expired entries read as absent (mirrors Redis TTL semantics).
    if isinstance(exp, float) and exp <= now:
        _mem_store.pop(k, None)
        _mem_store.pop(k + ":exp", None)
        prev = _MISSING
    else:
        prev = _mem_store.get(k, _MISSING)
    _mem_store[k] = value
    _mem_store.move_to_end(k)
    _mem_store[k + ":exp"] = now + float(_norm_ttl(ttl_sec))
    while len(_mem_store) > _MEM_MAX_KEYS * 2:  # cache keys come in pairs
        _mem_store.popitem(last=False)
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
            logger.debug(f"incr_counter redis failed ({exc}): mem mode")
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
            logger.debug(f"get_counter redis failed ({exc}): mem mode")
    ck = f"cnt:{key}"
    now = time.monotonic()
    exp = _mem_store.get(ck + ":exp")
    if isinstance(exp, float) and exp <= now:
        return 0
    try:
        return int(_mem_store.get(ck, 0) or 0)
    except (TypeError, ValueError):
        return 0
