"""Регресс на ошибку продакшена:

    WARNING | app.services.card_data - card: activity stats skipped:
    SQL expression for WHERE/HAVING role expected,
    got datetime.datetime(2026, 9, 25, 0, 0).

Причина: в ActivityRepository.daily_counts() граница периода попала в .where()
«голым» datetime (без сравнения с колонкой) — SQLAlchemy не принимает такое
условие, collect() для карточки ловил исключение и молча подставлял пустые
stats/daily (график активности за 7 дней был пустым).

Инвариант: daily_counts возвращает корректные подневные счётчики и НЕ бросает
исключение; card_data.collect() наполняет stats/daily.

Запуск из каталога bot/:  pytest tests/test_card_daily_counts.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import Base, ChatMessageLog, User          # noqa: E402
from app.db.repositories import ActivityRepository           # noqa: E402


def _log(user_id: int, msg_id: int, created_at: datetime) -> ChatMessageLog:
    return ChatMessageLog(
        user_id=user_id, chat_id=-1004467842206, message_id=msg_id,
        length=10, has_media=False, media_type="text", is_reply=False,
        mentions_count=0, is_counted=True, skip_reason=None,
        created_at=created_at,
    )


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        yield s
    await engine.dispose()


pytestmark = pytest.mark.asyncio


async def test_daily_counts_no_bare_datetime_in_where(session):
    now = datetime(2026, 10, 1, 14, 0)
    session.add_all([
        _log(1, 1, now),                        # сегодня
        _log(1, 2, datetime(2026, 9, 30)),      # вчера
        _log(1, 3, datetime(2026, 9, 25, 1)),   # ровно на границе окна (7 дней)
        _log(1, 4, datetime(2026, 9, 20)),      # до начала окна — не входит
        _log(2, 5, now),                        # другой пользователь
    ])
    await session.commit()

    repo = ActivityRepository(session)
    daily = await repo.daily_counts(1, days=7, since=now)  # не должно бросать

    assert daily == {"2026-10-01": 1, "2026-09-30": 1, "2026-09-25": 1}


async def test_collect_fills_stats_and_daily(session):
    from app.services.card_data import collect

    now = datetime.now()
    session.add(User(tg_id=26533379, username="jMangust", first_name="Mangust",
                     level=1, xp=30, coins=11, messages_count=1, created_at=now))
    session.add(_log(26533379, 1, now))
    await session.commit()

    data = await collect(session, 26533379)

    assert data["stats"], "stats пуст — запрос активности упал"
    assert data["daily"], "daily пуст — график активности в карточке сломан"
    assert sum(data["daily"].values()) >= 1
