"""Окружение Alembic для проекта TamaBot (async SQLAlchemy + MySQL/Postgres/SQLite).

Строка подключения берётся из app.config.get_settings() (DATABASE_URL / .env) —
в alembic.ini секретов нет. Для async-драйверов (aiomysql/asyncpg/aiosqlite)
URL преобразуется в sync-эквивалент: Alembic выполняет миграции синхронно.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# чтобы `import app...` работал при запуске alembic из каталога bot/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db.models import Base  # noqa: E402  (все таблицы регистрируются через метаданные)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url(url: str) -> str:
    """async-драйвер → sync-драйвер для миграций (aiomysql→pymysql и т.п.)."""
    return (url
            .replace("+aiomysql", "+pymysql")
            .replace("+asyncmy", "+pymysql")
            .replace("+asyncpg", "+psycopg2")
            .replace("+aiosqlite", ""))


def get_url() -> str:
    # env имеет приоритет над .env (CI/проде), как и в самом боте
    return _sync_url(os.environ.get("DATABASE_URL") or get_settings().database_url)


def run_migrations_offline() -> None:
    """Режим offline: генерируем SQL без подключения к БД (alembic upgrade --sql)."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        url=get_url(),
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite: batch mode для ALTER (в SQLite он ограничен);
            # partial unique индекс uq_pets_current_per_user (PG/SQLite)
            # автосравнением корректно не поддерживается — правим вручную
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
