"""Регресс-тесты v1.4.9: кросс-СУБД лёгкие миграции + фолбэк автора реакций.

Поймано продом (MySQL):
1. ALTER TABLE ... ADD COLUMN IF NOT EXISTS — синтаксис только PG/SQLite;
   на MySQL все команды падали с syntax error, молча проглатывались
   best-effort except → колонки pets.generation/is_archived не создавались,
   топ питомцев валился с OperationalError 1054 Unknown column.
2. Фолбэк автора сообщения для реакций звал несуществующий bot.get_message
   ('Bot' object has no attribute 'get_message') → реакции вне локального
   лога терялись.
"""
from __future__ import annotations

import ast
import asyncio
import re
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, ChatMessageLog, User
from app.db.repositories import ActivityRepository

APP = Path(__file__).resolve().parents[1] / "app"


# --------------------------------------------------------------------------
# 1. Синтаксис миграций: никакого IF NOT EXISTS в ALTER TABLE быть не должно
# --------------------------------------------------------------------------
def test_light_migrations_no_if_not_exists_in_real_sql():
    """Реальные SQL-строки в _LIGHT_COLUMNS/f-string миграций не должны
    использовать PG/SQLite-only синтаксис IF NOT EXISTS (MySQL падает)."""
    tree = ast.parse((APP / "main.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_light_migrations")
    # docstring функции не проверяем — только исполняемые строки/f-string'и
    sql_consts = []
    docstring = fn.body[0].value if (isinstance(fn.body[0], ast.Expr)
                                     and isinstance(fn.body[0].value, ast.Constant)) else None
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node is not docstring \
                and "ALTER TABLE" in node.value.upper():
            sql_consts.append(node.value)
    # строкового литерала с ALTER быть не должно — запрос собирается f-string'ом
    for s in sql_consts:
        assert "IF NOT EXISTS" not in s.upper(), f"MySQL не поддержит: {s!r}"
    body = ast.get_source_segment((APP / "main.py").read_text(encoding="utf-8"), fn)
    assert "f\"ALTER TABLE" in body or "f'ALTER TABLE" in body, \
        "ADD COLUMN должен собираться без IF NOT EXISTS"


def test_light_columns_catalog_matches_model():
    """Каталог лёгких миграций покрывает все новые колонки модели Pet."""
    from app.db.models import Pet
    tbl_cols = {c.name for c in Pet.__table__.columns}
    import app.main as m
    mig_cols = {c for cols in m._LIGHT_COLUMNS.values() for c, _t in cols}
    assert mig_cols <= tbl_cols, f"миграции завывают колонок, которых нет в модели: {mig_cols - tbl_cols}"


def test_light_migrations_use_inspector():
    """Недостающие колонки определяются через inspector — кросс-СУБД способ."""
    src = (APP / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    assert "_column_exists" in names and "_light_migrations" in names
    assert "sa_inspect" in src or "inspect(" in src


@pytest.mark.asyncio
async def test_light_migrations_add_columns_on_sqlite(tmp_path):
    """Эмуляция живой БД v1.4.6: таблица pets БЕЗ новых колонок → миграция
    должна добавить их идемпотентно (повторный запуск — без ошибок)."""
    from app.main import _light_migrations

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'m.db'}")
    async with engine.begin() as conn:
        # старая схема: pets без generation/is_archived/archived_at/archive_reason
        await conn.exec_driver_sql(
            """
            CREATE TABLE pets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name VARCHAR(64), species VARCHAR(32), level INTEGER DEFAULT 1,
                xp INTEGER DEFAULT 0, stage VARCHAR(32), hunger INTEGER,
                happiness INTEGER, energy INTEGER, hygiene INTEGER, health INTEGER,
                strength INTEGER, agility INTEGER, intellect INTEGER,
                is_sleeping BOOLEAN, sleep_until DATETIME, walk_until DATETIME,
                sick_since DATETIME, last_update DATETIME, born_at DATETIME,
                settings_extra TEXT
            )
            """
        )
    # первый прогон — добавляет 4 колонки + частичный индекс
    async with engine.begin() as conn:
        await _light_migrations(conn)
    # повторный прогон — идемпотентен (ничего не падает и не дублируется)
    async with engine.begin() as conn:
        await _light_migrations(conn)

    def _check(sync_conn):
        cols = {c["name"] for c in sa_inspect(sync_conn).get_columns("pets")}
        idxs = {i["name"] for i in sa_inspect(sync_conn).get_indexes("pets")}
        return cols, idxs

    async with engine.connect() as conn:
        cols, idxs = await conn.run_sync(_check)
    assert {"generation", "is_archived", "archived_at", "archive_reason"} <= cols
    assert "uq_pets_current_per_user" in idxs
    await engine.dispose()


# --------------------------------------------------------------------------
# 2. Реакции: автор из локального лога + forward-фолбэк вместо get_message
# --------------------------------------------------------------------------
def test_tracker_does_not_call_missing_get_message():
    src = (APP / "handlers" / "tracker.py").read_text(encoding="utf-8")
    assert ".get_message(" not in src, (
        "у Bot в aiogram 3.x нет метода get_message — использовать forward-фолбэк"
    )
    assert "_fetch_author_via_forward" in src
    assert "forward_message" in src


@pytest.mark.asyncio
async def test_get_message_author_from_log(tmp_path):
    from datetime import datetime, timezone

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'a.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(User(tg_id=111, username="author", first_name="A", created_at=now))
        await s.flush()
        repo = ActivityRepository(s)
        await repo.log_message(ChatMessageLog(
            user_id=111, chat_id=-100, message_id=42, length=20,
            has_media=False, media_type=None, is_reply=False,
            mentions_count=0, is_counted=True, skip_reason=None,
            created_at=now,
        ))
        assert await repo.get_message_author(-100, 42) == 111
        assert await repo.get_message_author(-100, 999) is None
        assert await repo.get_message_author(-200, 42) is None
    await engine.dispose()


def test_reaction_handler_uses_log_then_forward():
    """Хендлер: сначала БД, при miss — forward-фолбэк; self-реакция игнор."""
    src = (APP / "handlers" / "tracker.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "track_reaction_update"
    )
    body_src = ast.get_source_segment(src, fn)
    assert "get_message_author" in body_src      # источник №1 — лог
    assert "_fetch_author_via_forward" in body_src  # источник №2 — forward
    assert "to_user == from_user_id" in body_src     # антифрод само-реакций


def test_forward_fallback_deletes_copy_and_handles_errors():
    src = (APP / "handlers" / "tracker.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_fetch_author_via_forward"
    )
    body_src = ast.get_source_segment(src, fn)
    assert "delete_message" in body_src, "пересылку в ЛС бота надо удалять"
    assert "except" in body_src, "ошибки пересылки не должны ронять хендлер"
