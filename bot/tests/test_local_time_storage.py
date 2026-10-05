"""Хранение времени в БД = локальное время пользователей (TZ_OFFSET_HOURS).

Воспроизводит жалобу из продакшена:
    «В базе сейчас 09:52, хотя на часах 21:52»

Причина: после унификации TZ колонки created_at/last_seen заполнялись
UTC-моментом (datetime.now(timezone.utc)), а пользователи смотрели в базу
и видели сдвиг на -tz_offset часов. Исправление: models.utcnow() пишет
локальное время (TZ_OFFSET_HOURS от UTC), db_bound() подаёт границы без
сдвига, localize()/from_iso считают naive-метки локальными. Тест проверяет
инвариант:

    значение, записанное в created_at, совпадает с локальным временем
    (TZ_OFFSET_HOURS) с точностью до минут.

Тест фиксирует собственный TZ_OFFSET_HOURS=12 (историческая Камчатка),
независимо от значений по умолчанию в настройках.

Запуск из каталога bot/:  pytest tests/test_local_time_storage.py -v
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

KAM_OFFSET = timedelta(hours=12)


@pytest.fixture(autouse=True)
def force_tz_offset_12():
    """Фиксируем TZ_OFFSET_HOURS=12 для всех тестов этого модуля.

    Дефолт в настройках — UTC (0); тест проверяет инвариант «запись =
    локальное время» при ненулевом смещении, как в исторической конфигурации
    (Камчатка).
    """
    old = os.environ.get("TZ_OFFSET_HOURS")
    os.environ["TZ_OFFSET_HOURS"] = "12"
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ_OFFSET_HOURS", None)
        else:
            os.environ["TZ_OFFSET_HOURS"] = old
        get_settings.cache_clear()


def _fresh_db_url(tmp_path: Path, name: str) -> str:
    # изолированная sqlite-файловая база для каждого теста
    return f"sqlite+aiosqlite:///{tmp_path / name}"


@pytest.fixture()
def local_now_naive() -> datetime:
    """Опорное «локальное время Камчатки» независимо от кода приложения."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + KAM_OFFSET


def test_utcnow_writes_local_kamchatka_time(local_now_naive: datetime):
    from app.db.models import utcnow

    written = utcnow()
    # запись должна совпадать с локальным временем (не с UTC!)
    assert abs(written.replace(tzinfo=None) - local_now_naive) < timedelta(minutes=2), (
        "models.utcnow() должен писать локальное (камчатское) время: "
        f"записано {written}, локальное {local_now_naive}"
    )
    # и НЕ должен совпадать с UTC (это был баг: разница ровно 12 часов)
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs(written.replace(tzinfo=None) - utc_now) > timedelta(hours=6)


def test_db_bound_keeps_local_bounds(local_now_naive: datetime):
    from app.utils.local_time import db_bound

    aware_local = local_now_naive.replace(tzinfo=timezone(KAM_OFFSET))
    got = db_bound(aware_local)
    assert got.tzinfo is None
    assert abs(got - local_now_naive) < timedelta(seconds=5)
    # naive-граница считается уже локальной — возвращается как есть
    assert db_bound(local_now_naive) == local_now_naive


def test_created_at_column_matches_wall_clock(tmp_path, local_now_naive):
    """Сквозная проверка: ORM default попадает в базу как локальное время."""
    os.environ["DATABASE_URL"] = _fresh_db_url(tmp_path, "tzcheck.db")
    try:
        from app.config import get_settings

        get_settings.cache_clear()

        import asyncio

        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db.models import Base, User

        async def main() -> datetime:
            url = os.environ["DATABASE_URL"]
            engine = create_async_engine(url)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sf = async_sessionmaker(engine, expire_on_commit=False)
            async with sf() as s:
                u = User(tg_id=123456789, username="tz_probe", first_name="TZ")
                s.add(u)
                await s.commit()
                row = (await s.execute(
                    select(User.created_at).where(User.tg_id == 123456789)
                )).scalar_one()
            await engine.dispose()
            return row

        stored = asyncio.run(main())
        assert abs(stored.replace(tzinfo=None) - local_now_naive) < timedelta(minutes=2), (
            f"в базе {stored}, а на часах (Камчатка) {local_now_naive} — "
            "разница недопустима"
        )
    finally:
        os.environ.pop("DATABASE_URL", None)
        from app.config import get_settings

        get_settings.cache_clear()


def test_localize_treats_naive_as_local(local_now_naive: datetime):
    from app.utils.local_time import localize

    # метка из текущего хранилища (naive-локальная) отображается без сдвига
    shown = localize(local_now_naive)
    assert shown.strftime("%H:%M") == local_now_naive.strftime("%H:%M")
    # метка эпохи UTC-хранения (выглядит как прошлое для Камчатки) тоже
    # остаётся локальной — не сдвигается повторно
    old_local = local_now_naive - timedelta(days=3)
    assert localize(old_local).strftime("%Y-%m-%d %H:%M") == old_local.strftime("%Y-%m-%d %H:%M")
