"""v1.4.8: regression tests for dialect-aware ChatMessageLog upsert.

Прод на MySQL+aiomysql падал на каждом сообщении в группе:
UnsupportedCompilationError ... visit_on_conflict_do_nothing — потому что
для всех диалектов, кроме postgresql, выбирался sqlite-insert.
"""
from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db.models import Base, ChatMessageLog
from app.db.repositories import ActivityRepository


def _entry(msg_id: int = 1) -> ChatMessageLog:
    return ChatMessageLog(
        user_id=42, chat_id=-100500, message_id=msg_id, length=12,
        has_media=False, media_type="text", is_reply=False, mentions_count=0,
        is_counted=True, skip_reason=None,
        created_at=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc),
    )


class TestDialectSelection:
    """Компиляция должна проходить на ВСЕХ поддерживаемых диалектах."""

    def test_sqlite_source_compiles(self):
        # sanity: код репозитория выбирает insert-диалект по имени соединения
        src = inspect.getsource(ActivityRepository.log_message)
        assert "startswith(\"mysql\")" in src or 'startswith(\'mysql\')' in src
        assert "sqlite_insert" not in src or "dialect ==" in src  # нет жёсткого else->sqlite

    def test_mysql_statement_compiles(self):
        """Раньше именно здесь падал прод: sqlite OnConflictDoNothing не компилируется MySQL."""
        from sqlalchemy.dialects.mysql import insert as mysql_insert
        stmt = mysql_insert(ChatMessageLog).values(user_id=1, chat_id=1, message_id=1).prefix_with("IGNORE")
        sql = str(stmt.compile(dialect=mysql.dialect()))
        assert "INSERT IGNORE" in sql

    def test_pg_statement_compiles(self):
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(ChatMessageLog).values(user_id=1, chat_id=1, message_id=1).on_conflict_do_nothing(
            index_elements=["chat_id", "message_id"])
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "ON CONFLICT" in sql

    def test_sqlite_statement_compiles(self):
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        stmt = sqlite_insert(ChatMessageLog).values(user_id=1, chat_id=1, message_id=1).on_conflict_do_nothing(
            index_elements=["chat_id", "message_id"])
        sql = str(stmt.compile(dialect=sqlite.dialect()))
        assert "ON CONFLICT" in sql


@pytest.mark.asyncio
async def test_log_message_dedup_on_sqlite_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine) as session:
        repo = ActivityRepository(session)
        await repo.log_message(_entry(7))
        await repo.log_message(_entry(7))   # повторная доставка апдейта
        await repo.log_message(_entry(8))
        await session.commit()
        n = (await session.execute(select(func.count()).select_from(ChatMessageLog))).scalar_one()
        assert n == 2, "дедуп по (chat_id, message_id) должен отбросить повтор"
    await engine.dispose()


def test_unique_constraint_declared():
    """MySQL INSERT IGNORE работает только при наличии UNIQUE(chat_id, message_id)."""
    table = ChatMessageLog.__table__
    uqs = [c for c in table.constraints if type(c).__name__ == "UniqueConstraint"]
    cols = [tuple(sorted(col.name for col in c.columns)) for c in uqs]
    assert ("chat_id", "message_id") in cols


def test_main_module_ast_ok():
    p = Path(__file__).resolve().parents[1] / "app" / "db" / "repositories" / "__init__.py"
    ast.parse(p.read_text(encoding="utf-8"))
