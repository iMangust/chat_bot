"""Тесты кодировки: все MySQL-таблицы должны создаваться в utf8mb4,
иначе эмодзи в achievements.icon/items.name падают с ошибкой 1366."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from app.db.models import Base


def test_all_tables_utf8mb4():
    for table in Base.metadata.tables.values():
        ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))
        assert "utf8mb4" in ddl, f"таблица {table.name} не в utf8mb4:\n{ddl}"


def test_emoji_survives_sqlite_roundtrip(tmp_path):
    """Эмодзи корректно сохраняются (regression для 🐾💬🔥)."""
    import asyncio
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.db.models import Achievement

    async def _run():
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'e.db'}")
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(eng)
        async with sm() as s:
            s.add(Achievement(code="t", title="T", description="d", icon="🐾",
                              category="activity", condition_type="messages_total",
                              condition_value=1, reward_xp=1, reward_coins=1,
                              is_hidden=False, rarity="common"))
            await s.commit()
        async with sm() as s:
            a = (await s.execute(Achievement.__table__.select())).first()
            assert a.icon == "🐾"
        await eng.dispose()

    asyncio.run(_run())
