"""Регресс: корректная деградация при недоступном Redis (проблема №6).

Раньше было три бага:
  1. acquire_lock возвращал безусловный True без записи в _mem_store, а
     release_lock удалял несуществующий ключ — локи «сгорали» мгновенно и
     cron-джобы выполнялись несколько раз параллельно (дубли уведомлений).
  2. Каждое обращение к кулдаунам делало await ping() к мёртвому Redis —
     на пике нагрузки каждый инлайн-кнопочный клик ждал socket_timeout,
     бот становился дёрганым. Теперь circuit breaker: после падения Redis
     проверка повторяется не чаще одного раза в _redis_backoff_sec.
  3. Кулдауны, записанные в mem-фолбэк во время аварии, не зеркалятся в
     Redis; при восстановлении Redis отвечал «кулдауна нет» и пропускал
     дубли. Карантин (_mem_quarantine_until) держит приоритет mem до
     истечения самых длинных живых дедлайнов.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname("app"))
from app.utils import redis as ur


class DeadRedis:
    """Заглушка мёртвого Redis: считает обращения, чтобы проверить backoff."""

    def __init__(self):
        self.ping_calls = 0

    async def ping(self):
        self.ping_calls += 1
        raise ConnectionError("redis down")


def _reset_state():
    ur.redis_client = None
    ur._mem_store.clear()
    ur._warned_errors.clear()
    ur._redis_available = True
    ur._redis_last_check = 0.0
    ur._mem_quarantine_until = 0.0


def test_lock_expires_in_mem_fallback():
    """Лок в mem-фолбэке должен реально блокировать повтор до истечения TTL."""

    async def main():
        _reset_state()
        try:
            assert await ur.acquire_lock("decay", ttl_sec=60) is True
            # Повторный захват того же лока — отказ (было: всегда True).
            assert await ur.acquire_lock("decay", ttl_sec=60) is False
            # Освобождение снимает блок.
            await ur.release_lock("decay")
            assert await ur.acquire_lock("decay", ttl_sec=60) is True
            await ur.release_lock("decay")
            # Истёкший TTL переставляет лок.
            ur._mem_store["lock:decay"] = 0.0  # просроченный дедлайн
            assert await ur.acquire_lock("decay", ttl_sec=60) is True
        finally:
            _reset_state()

    asyncio.run(main())


def test_circuit_breaker_stops_hammering_dead_redis():
    """После обнаружения аварии ping повторяется не чаще раза в бэкофф-окно."""

    async def main():
        _reset_state()
        dead = DeadRedis()
        ur.redis_client = dead
        try:
            # Первое обращение: ping выполняется, Redis помечается мёртвым.
            ok = await ur.set_cooldown("cb:1:menu:main", 5)
            assert ok is True and dead.ping_calls == 1
            # Последующие обращения в окне бэкоффа — без ping вообще.
            for _ in range(50):
                await ur.set_cooldown("cb:1:menu:main", 5)
            assert dead.ping_calls == 1, (
                "circuit breaker должен глушить ping во время аварии"
            )
            # Кулдаун при этом работает корректно (второй клик заблокирован).
            assert await ur.set_cooldown("cb:1:menu:main", 5) is False
        finally:
            _reset_state()

    asyncio.run(main())


def test_mem_cooldown_survives_redis_recovery():
    """Кулдаун, записанный в mem во время аварии, не «забывается» сразу
    после восстановления Redis (карантин вместо тихих дублей)."""

    class FlakyRedis:
        def __init__(self):
            self.down = True
            self.store: dict[str, str] = {}

        async def ping(self):
            if self.down:
                raise ConnectionError("redis down")
            return True

        async def set(self, key, value, nx=False, ex=None, xx=False):
            if nx and key in self.store:
                return None
            self.store[key] = value
            return True

        async def ttl(self, key):
            return -2 if key not in self.store else 100

    async def main():
        _reset_state()
        flaky = FlakyRedis()
        ur.redis_client = flaky
        try:
            # Авария: первый клик уходит в mem, Redis помечён мёртвым.
            assert await ur.set_cooldown("msg:42", 60) is True
            assert await ur.set_cooldown("msg:42", 60) is False
            # Redis «починился». Без карантина NX-проверка прошла бы в
            # пустом Redis и пропустила дубль сообщения.
            flaky.down = False
            ur._redis_last_check = 0.0  # сбрасываем бэкофф — проверять можно
            assert await ur.set_cooldown("msg:42", 60) is False, (
                "после восстановления Redis mem-кулдаун обязан пережить его"
            )
            # По истечении карантина снова работаем с Redis.
            ur._mem_quarantine_until = 0.0
            assert "cd:msg:42" not in flaky.store
            # Новый ключ (не из mem) спокойно ставится в Redis.
            assert await ur.set_cooldown("msg:99", 60) is True
            assert flaky.store.get("cd:msg:99") == "1"
        finally:
            _reset_state()

    asyncio.run(main())


def test_get_cooldown_ttl_respects_mem_quarantine():
    """Кулдаун, записанный в mem во время аварии, после восстановления Redis
    должен показывать остаток из mem, а не 0 (иначе UI на долю секунды
    «отпускает» блокировку и пользователь жмёт повторно)."""

    class FlakyRedis:
        def __init__(self):
            self.down = True

        async def ping(self):
            if self.down:
                raise ConnectionError("redis down")
            return True

        async def set(self, key, value, nx=False, ex=None, xx=False):
            return True

        async def ttl(self, key):
            # Redis ничего не знает про кулдаун, записанный в mem-фолбэк.
            return -2

    async def main():
        _reset_state()
        flaky = FlakyRedis()
        ur.redis_client = flaky
        try:
            # Авария: кулдаун уходит в mem с TTL 60 сек.
            assert await ur.set_cooldown("msg:7", 60) is True
            # Redis «починился», бэкофф сброшен.
            flaky.down = False
            ur._redis_last_check = 0.0
            ttl = await ur.get_cooldown_ttl("msg:7")
            assert 50 <= ttl <= 60, (
                f"во время карантина остаток должен браться из mem, получено {ttl}"
            )
            # После окончания карантина — снова авторитет Redis (ключа там нет).
            ur._mem_quarantine_until = 0.0
            assert await ur.get_cooldown_ttl("msg:7") == 0
        finally:
            _reset_state()

    asyncio.run(main())

def test_mem_store_is_lru_bounded():
    """Мем-фолбэк не должен расти бесконечно при долгом аптайме без Redis."""

    async def main():
        _reset_state()
        old_max = ur._MEM_MAX_KEYS
        ur._MEM_MAX_KEYS = 32
        try:
            for i in range(200):
                await ur.set_cooldown(f"cb:{i}:btn", 60)
            assert len(ur._mem_store) <= 32, (
                f"_mem_store вырос до {len(ur._mem_store)} — LRU-eviction сломан"
            )
        finally:
            ur._MEM_MAX_KEYS = old_max
            _reset_state()

    asyncio.run(main())
