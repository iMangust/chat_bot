"""Окружение Alembic для миграций TamaBot.

Строка подключения берётся из app.config.get_settings() (env DATABASE_URL /
.env) — секреты не хранятся в alembic.ini. Драйвер принудительно заменяется на
синхронный: Alembic работает в sync-контексте, а async-драйверы (aiomysql,
aiosqlite, asyncpg) для него непригодны.

Запуск (из каталога bot/):
    alembic revision --autogenerate -m "описание"
    alembic upgrade head
    alembic stamp head          # пометить живую БД как актуальную
"""
from __future__ import annotations

import re
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Позволяет импортировать пакет app.* при запуске alembic из каталога bot/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db.models import Base  # noqa: E402  (импорт регистрирует все таблицы)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url(raw: str) -> str:
    """async-схему драйвера → синхронную для Alembic.

    aiomysql/asyncpg → pymysql; aiosqlite → стандартный sqlite (встроенный).
    """
    if "+aiosqlite" in raw:
        return raw.replace("+aiosqlite", "")
    return re.sub(r"\+(aiomysql|asyncpg)", "+pymysql", raw)


def run_migrations_offline() -> None:
    """Генерация SQL без подключения к БД (alembic upgrade --sql)."""
    url = _sync_url(get_settings().database_url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Миграции через live-подключение (sync engine)."""
    section = dict(config.get_section(config.config_ini_section) or {})
    section["sqlalchemy.url"] = _sync_url(get_settings().database_url)
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite не умеет ALTER COLUMN — batch mode обязателен для dev-БД.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
