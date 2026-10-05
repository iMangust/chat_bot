"""Регрессия Issue #11: конкурентность мерч-резервов (Race Condition).

Проверяет репозиторный слой MerchRepository напрямую на in-memory sqlite:
  1) reserve(): два параллельных резерва одного варианта — ровно один "ok",
     второй получает "already_reserved" (CAS через WHERE reserved_by IS NULL);
  2) confirm_sale(): двойное подтверждение продажи — списание происходит
     ровно один раз (stock/sold_count не уезжают в минус/дубль);
  3) cancel_reserve(): отмена засчитывается только реальному покупателю.

Ключевой инвариант: UPDATE ... WHERE с условием по зарезервированности +
проверка rowcount == 1. Без этого две кнопки «Подтвердить продажу», нажатые
админами одновременно, продали бы одну вещь дважды.
"""
from __future__ import annotations

import asyncio
import os
import sys

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT_DIR)

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("BOT_TOKEN", "123456:TEST-token")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("CHANNELS", "[]")
os.environ.setdefault("ADMIN_IDS", "[42]")

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

from app.config import get_settings as _gs  # noqa: E402
_gs.cache_clear()

import app.db.session as _dbs  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

_s = _gs()


def _fresh_engine():
    """Свежий in-memory движок на каждый тест (свой asyncio.run).

    StaticPool + check_same_thread=False: все сессии теста делят ОДНО общее
    соединение — иначе у каждого подключения своя пустая :memory: база
    («no such table»), а без StaticPool NullPool-движок переживает
    asyncio.run() между тестами и следующий падает с «Lock bound to a
    different event loop».
    """
    return create_async_engine(_s.database_url, poolclass=StaticPool,
                               connect_args={"check_same_thread": False})


def _reset_db():
    _dbs.engine = _fresh_engine()
    _dbs.session_factory = async_sessionmaker(
        _dbs.engine, class_=_dbs.AsyncSession, expire_on_commit=False)


async def _ensure_schema():
    from app.db.models import Base
    async with _dbs.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _mk_variant(stock: int = 3, color: str = "Чёрный") -> int:
    """Вариант в общем каталоге теста (уникальность: product+size+color)."""
    from app.db.repositories import MerchRepository
    await _ensure_schema()
    async with _dbs.session_factory() as s:
        repo = MerchRepository(s)
        cat = await repo.get_category("caps")
        if cat is None:
            cat = await repo.add_category("caps", "Кепки")
            prod = await repo.add_product(cat.id, "Тестовая кепка")
        else:
            prod = (await repo.products(cat.id))[0]
        v, _new = await repo.add_variant(prod.id, "M", color, 1000, stock)
        vid = v.id
        await s.commit()
    return vid


def test_concurrent_reserve_only_one_wins():
    """Два пользователя одновременно бронируют вариант — резерв получает ровно один.

    На sqlite+aiosqlite параллельные транзакции одного варианта физически
    последовательны (общее соединение StaticPool), поэтому «оба проиграли»
    невозможно — это и проверяет CAS-логику (UPDATE ... WHERE reserved_by IS
    NULL + rowcount == 1).
    """
    async def run():
        _reset_db()
        from app.db.repositories import MerchRepository
        vid = await _mk_variant(stock=3)

        async def reserve(user_id: int) -> str:
            async with _dbs.session_factory() as s:
                res = await MerchRepository(s).reserve(vid, user_id)
                await s.commit()
                return res

        r1, r2 = await asyncio.gather(reserve(101), reserve(102))
        oks = [r for r in (r1, r2) if r == "ok"]
        assert len(oks) == 1, f"резерв получили оба покупателя: {r1!r}, {r2!r}"
        loser = r1 if r1 != "ok" else r2
        assert loser == "already_reserved"

        async with _dbs.session_factory() as s:
            v = await MerchRepository(s).get_variant(vid)
            assert v.reserved_by in (101, 102)
    asyncio.run(run())


def test_double_confirm_sale_decrements_once():
    """Два параллельных подтверждения продажи одной брони — товар продан 1 раз."""
    async def run():
        _reset_db()
        from app.db.repositories import MerchRepository
        vid = await _mk_variant(stock=3)
        async with _dbs.session_factory() as s:
            assert await MerchRepository(s).reserve(vid, 777) == "ok"
            await s.commit()

        async def confirm() -> dict | None:
            async with _dbs.session_factory() as s:
                res = await MerchRepository(s).confirm_sale(vid)
                await s.commit()
                return res

        c1, c2 = await asyncio.gather(confirm(), confirm())
        assert sum(1 for c in (c1, c2) if c is not None) == 1, \
            f"продажа проведена более одного раза: {c1!r}, {c2!r}"

        async with _dbs.session_factory() as s:
            v = await MerchRepository(s).get_variant(vid)
            assert v.stock == 2, f"остаток списан неверно: {v.stock}"
            assert v.sold_count == 1, f"sold_count задвоен: {v.sold_count}"
            assert v.reserved_by is None
    asyncio.run(run())


def test_confirm_sale_zero_stock_is_noop():
    """Подтверждение при нулевом остатке не списывает и не «оживляет» бронь."""
    async def run():
        _reset_db()
        from app.db.repositories import MerchRepository
        vid = await _mk_variant(stock=1)
        async with _dbs.session_factory() as s:
            assert await MerchRepository(s).reserve(vid, 777) == "ok"
            await s.commit()
        async with _dbs.session_factory() as s:
            assert await MerchRepository(s).confirm_sale(vid) is not None
            await s.commit()
        # остатки 0; кто-то повторно тычет sold (бронь уже снята → None)
        async with _dbs.session_factory() as s:
            assert await MerchRepository(s).confirm_sale(vid) is None
            await s.commit()
        async with _dbs.session_factory() as s:
            v = await MerchRepository(s).get_variant(vid)
            assert v.stock == 0 and v.sold_count == 1
    asyncio.run(run())


def test_cancel_reserve_only_affects_existing_reservation():
    async def run():
        _reset_db()
        from app.db.repositories import MerchRepository
        vid = await _mk_variant(stock=2)
        async with _dbs.session_factory() as s:
            repo = MerchRepository(s)
            assert await repo.cancel_reserve(vid) is None, \
                "отмена несуществующей брони должна быть no-op"
            await s.commit()
        async with _dbs.session_factory() as s:
            assert await MerchRepository(s).reserve(vid, 555) == "ok"
            await s.commit()
        async with _dbs.session_factory() as s:
            res = await MerchRepository(s).cancel_reserve(vid)
            await s.commit()
        assert res == {"variant_id": vid, "buyer": 555}
        async with _dbs.session_factory() as s:
            v = await MerchRepository(s).get_variant(vid)
            assert v.reserved_by is None and v.stock == 2, "отмена не вернула доступность"
    asyncio.run(run())
