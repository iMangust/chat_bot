from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand, BotCommandScopeAllChatAdministrators, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
from loguru import logger

from sqlalchemy import inspect as sa_inspect

from app.config import get_settings
from app.db.models import Base, utcnow
from app.db.session import DbMiddleware, engine, session_factory
from app.handlers import (access as access_handlers, admin, arena, errors,
                          events, games, manual, merch, shop, social,
                          start, stats, tamagotchi, tracker)
from app.middlewares.gate import AccessGateMiddleware
from app.middlewares.nav_stack import NavStackMiddleware
from app.middlewares.throttle import ThrottleMiddleware
from app.db.repositories import seed_merch_catalog
from app.handlers.shop import seed_items
from app.services.achievements import seed_achievements
from app.tasks.scheduler import build_scheduler
from app.utils.redis import close_redis, init_redis

def setup_logging(level: str) -> None:
    logger.remove()
    logger.add(sys.stderr, level=level,
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | "
                      "<cyan>{name}</cyan> - <level>{message}</level>")
    logger.add("logs/bot_{time:YYYY-MM-DD}.log", rotation="1 day", retention="14 days",
               level="DEBUG", encoding="utf-8")
    # Telethon пишет в стандартный logging «Telegram is having internal issues
    # PersistentTimestampOutdatedError» пачками при каждом сбое синхронизации
    # pts — это штатная самовосстанавливающаяся ситуация. Перехватываем логи
    # telethon через loguru и глушим именно этот шум (остальные сообщения
    # telethon остаются видны).
    import logging

    class _TelethonIntercept(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                msg = record.getMessage()
            except Exception:
                return
            if "PersistentTimestampOutdatedError" in msg or \
                    "Persistent timestamp outdated" in msg:
                logger.debug("telethon (pts): {}", msg)
                return
            logger.bind(telethon=True).log(
                max(record.levelno, 10), "telethon: {}", msg)

    h = logging.getLogger("telethon")
    h.handlers = [x for x in h.handlers if not isinstance(x, _TelethonIntercept)]
    h.addHandler(_TelethonIntercept())
    h.setLevel(logging.INFO)
    h.propagate = False

    # APScheduler пишет WARNING вида «Run time of job "flush_notifications
    # (trigger: interval[0:01:00], ...)" was missed by 0:00:01.25» при любом
    # запаздывании тика — даже на секунду, когда задача всё равно будет
    # выполнена (у неё coalesce=True и misfire_grace_time). Это штатная
    # саморегуляция планировщика, а не ошибка: глушим именно эти сообщения,
    # реальные пропуски (за пределами grace) остаются видны как ERROR.
    class _SchedulerMissedFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            try:
                msg = record.getMessage()
            except Exception:
                return True
            return "was missed by" not in msg

    sched_log = logging.getLogger("apscheduler.executors.default")
    sched_log.addFilter(_SchedulerMissedFilter())

def _make_fsm_storage(redis_url: str):
    from aiogram.fsm.storage.memory import MemoryStorage
    try:
        from redis.asyncio import ConnectionPool, Redis
        pool = ConnectionPool.from_url(
            redis_url,
            protocol=2,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
        storage = RedisStorage(redis=Redis(connection_pool=pool))
        logger.info("FSM: RedisStorage (protocol=2/RESP2 — совместимо со старым Redis/Memurai)")
        return storage
    except Exception as exc:
        logger.warning(f"Redis недоступен для FSM ({exc!r}) — использую MemoryStorage "
                       f"(стейты сбрасываются при рестарте; для dev допустимо)")
        return MemoryStorage()

async def probe_fsm_storage(storage):
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    if isinstance(storage, MemoryStorage):
        return storage
    try:
        await storage.redis.ping()
        await storage.get_state(key=StorageKey(bot_id=0, chat_id=0, user_id=0))
        return storage
    except Exception as exc:
        logger.warning(
            f"FSM-хранилище (Redis) проверено и отключено ({type(exc).__name__}: {exc}) — "
            f"перехожу на MemoryStorage (кулдауны стейтов сбрасываются при рестарте)")
        return MemoryStorage()

async def _column_exists(conn, table: str, column: str) -> bool:
    def _check(sync_conn) -> bool:
        try:
            cols = {c["name"] for c in sa_inspect(sync_conn).get_columns(table)}
        except Exception:
            return False
        return column in cols

    return bool(await conn.run_sync(_check))

async def _index_exists(conn, index_name: str) -> bool:
    def _check(sync_conn) -> bool:
        insp = sa_inspect(sync_conn)
        for tbl in insp.get_table_names():
            if any(idx.get("name") == index_name for idx in insp.get_indexes(tbl)):
                return True
        return False

    return bool(await conn.run_sync(_check))

# Таблицы, которые могут отсутствовать в старых БД полностью (create_all их
# не досоздаёт, если таблица уже есть с другим набором колонок). Создаются
# через Base.metadata.create_all(tables=[...]) по факту отсутствия.
_LIGHT_TABLES: tuple[str, ...] = ("events",)

_LIGHT_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "pets": [
        ("generation", "INTEGER NOT NULL DEFAULT 1"),
        ("is_archived", "BOOLEAN NOT NULL DEFAULT 0"),
        ("archived_at", "DATETIME"),
        ("archive_reason", "VARCHAR(32)"),
        ("sleep_started_at", "DATETIME"),
        ("walk_start_at", "DATETIME"),
    ],
    "merch_variants": [
        ("photo_file_id", "VARCHAR(256)"),
        # «Снимок» покупателя на момент брони — нужен для кликабельного
        # уведомления о новой броне (имя + username).
        ("buyer_name", "VARCHAR(128) NOT NULL DEFAULT ''"),
        ("buyer_username", "VARCHAR(64)"),
    ],
    "users": [
        ("welcome_shown", "BOOLEAN NOT NULL DEFAULT 0"),
        # Колонки из более новых версий модели, которых нет в старых БД
        # (create_all не меняет существующие таблицы). Без них любой
        # SELECT/INSERT по users падает с OperationalError/ProgrammingError
        # («no such column users.created_at» / «column users.created_at does
        # not exist») — ровно тот каскад ошибок, что был в логах на /start.
        ("created_at", "DATETIME"),
        ("updated_at", "DATETIME"),
        ("messages_count", "INTEGER NOT NULL DEFAULT 0"),
        ("reactions_given", "INTEGER NOT NULL DEFAULT 0"),
        ("reactions_received", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "notifications_queue": [
        # клавиатура уведомления («Продолжить день» в дневном отчёте)
        ("payload_json", "TEXT"),
    ],
}

def _cs_new_ddl(dialect: str) -> str:
    if dialect == "postgresql":
        return """
            CREATE TABLE channel_subscribers_new (
                user_id BIGINT NOT NULL PRIMARY KEY,
                username VARCHAR(64),
                first_name VARCHAR(128) NOT NULL DEFAULT '',
                first_seen TIMESTAMP NOT NULL,
                last_seen_at TIMESTAMP NOT NULL,
                chats JSONB NOT NULL DEFAULT '[]',
                ever_contacted BOOLEAN NOT NULL DEFAULT FALSE,
                last_contact_at TIMESTAMP
)"""
    if dialect in ("mysql", "mariadb"):
        return """
            CREATE TABLE channel_subscribers_new (
                user_id BIGINT NOT NULL PRIMARY KEY,
                username VARCHAR(64),
                first_name VARCHAR(128) NOT NULL DEFAULT '',
                first_seen DATETIME(6) NOT NULL,
                last_seen_at DATETIME(6) NOT NULL,
                chats JSON NOT NULL,
                ever_contacted TINYINT(1) NOT NULL DEFAULT 0,
                last_contact_at DATETIME(6)
)"""
    return """
        CREATE TABLE channel_subscribers_new (
            user_id BIGINT NOT NULL PRIMARY KEY,
            username VARCHAR(64),
            first_name VARCHAR(128) NOT NULL DEFAULT '',
            first_seen DATETIME NOT NULL,
            last_seen_at DATETIME NOT NULL,
            chats TEXT NOT NULL DEFAULT '[]',
            ever_contacted BOOLEAN NOT NULL DEFAULT 0,
            last_contact_at DATETIME
)"""

def _migrate_sqlite_transfer_v20(
    old_pk_pairs: bool, has_contact: bool, cols: set[str] | None = None
) -> list[str]:
    def col(name: str, agg: str = "") -> str:
        if cols is not None and name not in cols:
            return "NULL" if not agg else f"{agg}(NULL)"
        return f"{agg}(o.{name})" if agg else (f"o.{name}" if old_pk_pairs else name)

    contact_expr = ("MAX(ever_contacted)" if has_contact else "0") if old_pk_pairs \
        else ("ever_contacted" if has_contact else "0")
    if old_pk_pairs:
        return [
            """
            CREATE TEMP TABLE _cs_chats AS
            SELECT DISTINCT user_id, chat_id FROM channel_subscribers
            WHERE chat_id IS NOT NULL AND chat_id <> 0""",
            f"""
            INSERT INTO channel_subscribers_new
                (user_id, username, first_name, first_seen, last_seen_at,
                 chats, ever_contacted, last_contact_at)
            SELECT o.user_id, MAX(o.username), {col('first_name', 'MAX')},
                   MIN({col('joined_at')}), {col('last_seen_at', 'MAX')},
                   CASE WHEN NOT EXISTS (SELECT 1 FROM _cs_chats c
                                    WHERE c.user_id = o.user_id)
                        THEN '[]'
                        ELSE (SELECT '[' || GROUP_CONCAT(c2.chat_id, ',') || ']'
                              FROM (SELECT DISTINCT chat_id
                                    FROM _cs_chats
                                    WHERE user_id = o.user_id) c2)
                   END,
                   COALESCE({contact_expr}, 0), {col('last_contact_at', 'MAX')}
            FROM channel_subscribers o
            GROUP BY o.user_id""",
            "DROP TABLE _cs_chats",
        ]
    chats_sel = ("COALESCE(chats, '[]')" if cols is None or "chats" in cols
                else "'[]'")
    return [
        f"""
        INSERT INTO channel_subscribers_new
            (user_id, username, first_name, first_seen, last_seen_at,
             chats, ever_contacted, last_contact_at)
        SELECT user_id, username, {col('first_name')}, {col('first_seen')},
               {col('last_seen_at')},
               {chats_sel},
               COALESCE({contact_expr}, 0), {col('last_contact_at')}
        FROM channel_subscribers""",
    ]

async def _migrate_channel_subscribers_v20(engine) -> None:
    from sqlalchemy import text

    def _table_cols(sync_conn) -> set[str]:
        insp = sa_inspect(sync_conn)
        if "channel_subscribers" not in insp.get_table_names():
            return set()
        try:
            return {c["name"] for c in insp.get_columns("channel_subscribers")}
        except Exception:
            return set()

    async with engine.begin() as conn:
        dialect = conn.dialect.name
        try:
            cols = await conn.run_sync(_table_cols)
            if not cols:
                return
            if not (cols & {"welcome_status", "welcomed_at", "dm_tries"}):
                if dialect == "sqlite":
                    def _chats_is_json_type(sync_conn) -> bool:
                        insp = sa_inspect(sync_conn)
                        for c in insp.get_columns("channel_subscribers"):
                            if c["name"] == "chats":
                                return type(c["type"]).__name__ == "JSON"
                        return False
                    if await conn.run_sync(_chats_is_json_type):
                        await conn.execute(text(_cs_new_ddl("sqlite")))
                        await conn.execute(text(
                            """INSERT INTO channel_subscribers_new
                               (user_id, username, first_name, first_seen,
                                last_seen_at, chats, ever_contacted,
                                last_contact_at)
                               SELECT user_id, username, first_name, first_seen,
                                      last_seen_at,
                                      COALESCE(NULLIF(chats, ''), '[]'),
                                      ever_contacted, last_contact_at
                               FROM channel_subscribers"""))
                        await conn.execute(text("DROP TABLE channel_subscribers"))
                        await conn.execute(text(
                            "ALTER TABLE channel_subscribers_new "
                            "RENAME TO channel_subscribers"))
                        logger.info("миграция v2.0.2: channel_subscribers.chats "
                                    "перестроена JSON → TEXT")
                return
            old_pk_pairs = "chat_id" in cols
            has_contact = "ever_contacted" in cols
            contact_expr = ("MAX(ever_contacted)" if old_pk_pairs and has_contact
                            else ("ever_contacted" if has_contact else "0"))
            contact_val = ("bool_or(ever_contacted)" if dialect == "postgresql"
                           and old_pk_pairs and has_contact else
                           ("CAST(ever_contacted AS BOOLEAN)"
                            if has_contact else "FALSE"))
            new_ddl = _cs_new_ddl(dialect)
            common_idx = [
                "CREATE INDEX IF NOT EXISTS ix_channel_subscribers_first_seen "
                "ON channel_subscribers (first_seen)",
            ]

            async def _swap_and_finish() -> None:
                await conn.execute(text("DROP TABLE channel_subscribers"))
                await conn.execute(text(
                    "ALTER TABLE channel_subscribers_new RENAME TO channel_subscribers"))
                for idx_sql in common_idx:
                    with contextlib.suppress(Exception):
                        await conn.execute(text(idx_sql))

            if dialect == "sqlite":
                await conn.execute(text(new_ddl))
                for stmt in _migrate_sqlite_transfer_v20(old_pk_pairs, has_contact, cols):
                    await conn.execute(text(stmt))
                await _swap_and_finish()
            elif dialect == "postgresql":
                await conn.execute(text(new_ddl))
                chats_sel = ("COALESCE(jsonb_agg(DISTINCT c.chat_id) "
                             "FILTER (WHERE c.chat_id IS NOT NULL AND c.chat_id <> 0), "
                             "'[]'::jsonb)") if old_pk_pairs else "COALESCE(o.chats, '[]'::jsonb)"
                grp = ("o.user_id" if not old_pk_pairs else
                       "o.user_id, o.username, o.first_name, o.last_contact_at")
                join = ("LEFT JOIN channel_subscribers c ON c.user_id = o.user_id"
                        if old_pk_pairs else "")
                await conn.execute(text(f"""
                    INSERT INTO channel_subscribers_new
                        (user_id, username, first_name, first_seen, last_seen_at,
                         chats, ever_contacted, last_contact_at)
                    SELECT o.user_id, MAX(o.username), MAX(o.first_name),
                           MIN(o.first_seen), MAX(o.last_seen_at),
                           {chats_sel.replace('c.chat_id', 'c.chat_id') if old_pk_pairs else 'COALESCE(MAX(o.chats), chr(39)||chr(91)||chr(39)::jsonb)'},
                           COALESCE({contact_val}, FALSE), MAX(o.last_contact_at)
                    FROM channel_subscribers o {join}
                    GROUP BY {grp}"""))
                await _swap_and_finish()
            else:
                await conn.execute(text(new_ddl))
                if old_pk_pairs:
                    chats_expr = """(
                               SELECT COALESCE(JSON_ARRAYAGG(cc.chat_id), JSON_ARRAY)
                               FROM (SELECT DISTINCT user_id, chat_id
                                     FROM channel_subscribers
                                     WHERE chat_id IS NOT NULL AND chat_id <> 0) cc
                               WHERE cc.user_id = o.user_id)"""
                else:
                    chats_expr = "COALESCE(o.chats, JSON_ARRAY())"
                await conn.execute(text(f"""
                    INSERT INTO channel_subscribers_new
                        (user_id, username, first_name, first_seen, last_seen_at,
                         chats, ever_contacted, last_contact_at)
                    SELECT o.user_id, MAX(o.username), MAX(o.first_name),
                           MIN(o.first_seen), MAX(o.last_seen_at),
                           {chats_expr},
                           COALESCE(CAST(MAX({contact_expr}) AS UNSIGNED), 0),
                           MAX(o.last_contact_at)
                    FROM channel_subscribers o
                    GROUP BY o.user_id"""))
                await _swap_and_finish()
            logger.info("миграция v2.0: channel_subscribers → чистый реестр "
                        "членства (PK=user_id, chats; welcome-колонки удалены)")
        except Exception as exc:
            logger.warning("миграция channel_subscribers v2.0 пропущена: {}: {}",
                           type(exc).__name__, str(exc)[:200])

async def _backfill_subscriber_chats_v202(engine) -> None:
    from sqlalchemy import text

    st = get_settings()
    ids = {int(c) for c in (st.tracked_chat_ids or []) if c}
    if st.channel_chat_id:
        ids.add(int(st.channel_chat_id))
    if not ids:
        return
    placeholders = ", ".join(f":c{i}" for i in range(len(ids)))
    params: dict = {f"c{i}": cid for i, cid in enumerate(sorted(ids))}

    async with engine.begin() as conn:
        try:
            res = await conn.execute(text(
                f"""SELECT user_id, chat_id FROM chat_messages_log
                    WHERE chat_id IN ({placeholders})
                    GROUP BY user_id, chat_id"""), params)
            rows = list(res.fetchall())
        except Exception as exc:
            logger.info("backfill chats v2.0.2: chat_messages_log недоступны "
                        "({}), пропускаем", type(exc).__name__)
            return
        try:
            res = await conn.execute(text(
                f"""SELECT from_user AS user_id, chat_id FROM reactions_log
                    WHERE chat_id IN ({placeholders})
                    GROUP BY from_user, chat_id"""), params)
            rows += list(res.fetchall())
        except Exception:
            pass
        if not rows:
            return
        import json as _json
        import re as _re

        def _normalize_chats(raw) -> str:
            if raw is None:
                return "[]"
            if isinstance(raw, (list, tuple)):
                nums = [int(x) for x in raw]
            else:
                try:
                    parsed = _json.loads(raw)
                    if not isinstance(parsed, (list, tuple)):
                        raise ValueError
                    nums = [int(x) for x in parsed]
                except (ValueError, TypeError):
                    nums = [int(x) for x in _re.findall(r"-?\d+", str(raw))]
            return _json.dumps(sorted(set(nums)))

        fixed = 0
        rows_empty = await conn.execute(text(
            """SELECT user_id, chats FROM channel_subscribers
               WHERE chats IS NULL OR chats = '' OR chats = '[]'"""))
        have_by_user: dict[int, list[int]] = {}
        for uid, raw in rows_empty.all():
            norm = _normalize_chats(raw)
            if norm != "[]":
                await conn.execute(text(
                    "UPDATE channel_subscribers SET chats = :chats "
                    "WHERE user_id = :uid"),
                    {"uid": int(uid), "chats": norm})
                fixed += 1
            else:
                have_by_user[int(uid)] = []
        by_user: dict[int, list[int]] = {}
        for uid, cid in rows:
            by_user.setdefault(int(uid), []).append(int(cid))
        # Dialect-aware upsert: «INSERT OR IGNORE» — синтаксис SQLite и на
        # MySQL/MariaDB даёт синтаксическую ошибку (1064). Для MySQL-диалекта
        # используем INSERT IGNORE.
        insert_verb = ("INSERT IGNORE INTO"
                       if conn.dialect.name in ("mysql", "mariadb")
                       else "INSERT OR IGNORE INTO")
        for uid, chats in by_user.items():
            if uid in have_by_user:
                continue
            res = await conn.execute(text(f"""
                {insert_verb} channel_subscribers
                    (user_id, username, first_name, first_seen, last_seen_at,
                     chats, ever_contacted, last_contact_at)
                SELECT :uid, NULL, '', :ts, :ts,
                       :chats, 0, NULL
                WHERE NOT EXISTS (SELECT 1 FROM channel_subscribers
                                  WHERE user_id = :uid)"""),
                {"uid": uid, "chats": _json.dumps(sorted(set(chats))),
                 "ts": utcnow().strftime("%Y-%m-%d %H:%M:%S")})
            fixed += res.rowcount or 0
        for uid, chats in ((u, c) for u, c in by_user.items() if u in have_by_user):
            res = await conn.execute(text(
                """UPDATE channel_subscribers
                   SET chats =:chats
                   WHERE user_id =:uid
                     AND (chats IS NULL OR chats = '' OR chats = '[]')"""),
                {"uid": uid, "chats": _json.dumps(sorted(set(chats)))})
            fixed += res.rowcount or 0
        if fixed:
            logger.info("миграция v2.0.2: чат(ы) восстановлены из истории "
                        "сообщений/реакций для {} подписчик(ов)", fixed)

def _table_exists_in_db(db_url: str, table: str) -> bool:
    """Проверка наличия таблицы без привязки к активным connection-pool'ам.

    Отдельный engine + мгновенное закрытие: безопасно вызывать даже из
    работающего цикла событий (после drop_engine/pool restart).
    """
    import asyncio

    from sqlalchemy import inspect as _sa_inspect
    from sqlalchemy.ext.asyncio import create_async_engine as _cae

    async def _check() -> bool:
        eng = _cae(db_url)
        try:
            async with eng.connect() as conn:
                return bool(await conn.run_sync(
                    lambda sc: _sa_inspect(sc).has_table(table)))
        finally:
            await eng.dispose()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(1) as ex:
            fut = ex.submit(asyncio.run, _check())
            return fut.result(timeout=30)
    return asyncio.run(_check())


async def ensure_events_table(engine_obj=None) -> bool:
    """Гарантирует существование таблицы events (создаёт по модели, если её нет).

    Возвращает True, если была создана заново. Идемпотентно; ошибки проглатывает
    с логом — вызывается и из on_startup, и «на лету» из хендлера мероприятий.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError, ProgrammingError

    eng = engine_obj or engine
    created = False
    try:
        async with eng.begin() as conn:
            exists = await conn.run_sync(
                lambda sc: sa_inspect(sc).has_table("events"))
            if not exists:
                from app.db.models import Base, Event
                await conn.run_sync(Base.metadata.create_all,
                                    tables=[Event.__table__])
                created = True
                logger.warning("миграция v2.0.3: таблица 'events' отсутствовала "
                               "— создана по модели")
            else:
                # на случай неполной старой таблицы — досоздать ключевые колонки
                cols = {c["name"] for c in (await conn.run_sync(
                    lambda sc: sa_inspect(sc).get_columns("events")))}
                for col, ddl in (("date", "VARCHAR(10) NOT NULL DEFAULT ''"),
                                 ("icon", "VARCHAR(16) NOT NULL DEFAULT '🎪'"),
                                 ("going", "JSON")):
                    if col not in cols:
                        try:
                            await conn.execute(text(
                                f"ALTER TABLE events ADD COLUMN {col} {ddl}"))
                            logger.info("миграция v2.0.3: events.{} добавлена", col)
                        except Exception as exc:
                            logger.debug("миграция events.{} пропущена: {}",
                                         col, type(exc).__name__)
    except (OperationalError, ProgrammingError) as exc:
        logger.error("ensure_events_table failed: {}: {}",
                    type(exc).__name__, str(exc)[:200])
    except Exception as exc:  # pragma: no cover - защита от редких диалектов
        logger.error("ensure_events_table unexpected: {}: {}",
                    type(exc).__name__, str(exc)[:200])
    return created


async def _light_migrations(conn) -> None:
    from sqlalchemy import text

    dialect = conn.dialect.name

    # Таблицы целиком (могут отсутствовать в старых БД полностью).
    for tname in _LIGHT_TABLES:
        exists = await conn.run_sync(
            lambda sc, t=tname: sa_inspect(sc).has_table(t))
        if not exists:
            from app.db.models import Base
            table_obj = Base.metadata.tables.get(tname)
            if table_obj is not None:
                await conn.run_sync(Base.metadata.create_all,
                                    tables=[table_obj])
                logger.warning("лёгкая миграция: таблица '{}' отсутствовала — "
                               "создана по модели ({})", tname, dialect)

    for table, columns in _LIGHT_COLUMNS.items():
        tbl_exists = await conn.run_sync(
            lambda sc, t=table: sa_inspect(sc).has_table(t))
        if not tbl_exists:
            continue  # create_all создаст её целиком вместе со всеми колонками
        for column, ddl_type in columns:
            if not await _column_exists(conn, table, column):
                sql = f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"
                try:
                    await conn.execute(text(sql))
                    logger.info("лёгкая миграция: {}.{} добавлена ({})", table, column, dialect)
                except Exception as exc:
                    logger.warning(
                        "лёгкая миграция {} не применена ({}): {}",
                        sql[:80], type(exc).__name__, exc,
                    )

    idx_name = "uq_pets_current_per_user"
    if dialect in {"postgresql", "sqlite"} and not await _index_exists(conn, idx_name):
        try:
            await conn.execute(text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {idx_name} "
                "ON pets (user_id) WHERE COALESCE(is_archived, 0) = 0"
            ))
        except Exception as exc:
            logger.debug("частичный индекс {} пропущен: {}", idx_name, type(exc).__name__)

async def _renumber_merch_category_codes(engine) -> None:
    """Старые символьные коды категорий (hoodie/tshirt/bag) -> порядковые id1, id2, ..."""
    from sqlalchemy import text as _text
    try:
        async with engine.begin() as conn:
            rows = list(await conn.execute(_text(
                "SELECT id, code FROM merch_categories ORDER BY position, id")))
            for i, (cid, code) in enumerate(rows):
                want = f"id{i + 1}"
                if code == want:
                    continue
                taken = await conn.execute(
                    _text("SELECT 1 FROM merch_categories WHERE code = :c AND id <> :i"),
                    {"c": want, "i": cid})
                if taken.first():
                    continue
                await conn.execute(
                    _text("UPDATE merch_categories SET code = :c WHERE id = :i"),
                    {"c": want, "i": cid})
    except Exception as exc:
        logger.debug("renumber merch category codes skipped: {}", type(exc).__name__)

async def on_startup(bot: Bot) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _light_migrations(conn)
    # Самолечение «мертвой» кнопки «Мероприятия»: в старых/битых БД таблицы
    # events может не быть вовсе (SELECT падает с OperationalError).
    await ensure_events_table(engine)
    await _renumber_merch_category_codes(engine)
    await _migrate_channel_subscribers_v20(engine)
    await _backfill_subscriber_chats_v202(engine)
    async with session_factory() as session:
        await seed_achievements(session)
        await seed_items(session)
        seeded = await seed_merch_catalog(session)
        if seeded:
            logger.info("merch catalog seeded: {} variants", seeded)
        await session.commit()
    await bot.delete_my_commands()
    await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
    await bot.delete_my_commands(scope=BotCommandScopeAllChatAdministrators())
    private_scope = BotCommandScopeAllPrivateChats()
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="pet", description="🐾 Питомец"),
            BotCommand(command="stats", description="📊 Моя статистика"),
            BotCommand(command="ach", description="🏆 Достижения"),
            BotCommand(command="top", description="🏅 Топы"),
            BotCommand(command="arena", description="⚔️ Арена питомцев"),
            BotCommand(command="card", description="🖼 Карточка профиля"),
            BotCommand(command="award", description="🎁 Итоги недели"),
            BotCommand(command="weather", description="🌦️ Погода и эффекты"),
            BotCommand(command="settings", description="⚙️ Настройки"),
            BotCommand(command="help", description="❓ Справка"),
        ],
        scope=private_scope,
    )
    logger.info("✅ bot started")
    try:
        from app.services.mtproto_sync import autosync_if_configured
        st = get_settings()
        if st.mtproto_autosync:
            res = await autosync_if_configured()
            if res is not None:
                logger.info("🔄 MTProto autosync: {}", res)
        else:
            asyncio.create_task(autosync_guard())
    except Exception as exc:
        logger.info("MTProto autosync недоступен ({}) — работаю только на Bot API",
                    type(exc).__name__)

async def autosync_guard() -> None:
    with contextlib.suppress(Exception):
        from app.services.mtproto_sync import autosync_if_configured
        await autosync_if_configured()

async def main() -> None:
    settings = get_settings()
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN не задан — скопируйте .env.example в .env")
    setup_logging(settings.log_level)

    init_redis()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # Юзернейм бота нужен для deep-link кнопок (t.me/<bot>?start=...).
    # Если в .env не задан — определяем через get_me() и пишем в settings,
    # чтобы _bot_link() в merch/events мог его читать. Ошибку сети не считаем
    # фатальной: без username просто не будет URL-кнопок-ссылок на бота.
    if not (settings.bot_username or "").strip():
        try:
            me = await bot.get_me()
            if me.username:
                settings.bot_username = me.username
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_me for bot_username failed: %s", exc)
    storage = _make_fsm_storage(settings.redis_url)
    storage = await probe_fsm_storage(storage)

    dp = Dispatcher(storage=storage)
    dp.update.outer_middleware(DbMiddleware())
    from app.middlewares.theme import (ThemeMiddleware, ThemeErrorMiddleware,
                                       ThemeGuardMiddleware)
    dp.update.outer_middleware(ThemeMiddleware())
    dp.errors.middleware(ThemeErrorMiddleware())
    # Inner-гарант: прямо перед каждым хендлером убеждаемся, что в контексте
    # стоит тема адресата апдейта (см. docstring ThemeGuardMiddleware).
    for _obs in (dp.callback_query, dp.message, dp.edited_message):
        _obs.middleware.register(ThemeGuardMiddleware())
    dp.update.outer_middleware(access_handlers.AccessEventsMiddleware())
    dp.update.outer_middleware(AccessGateMiddleware())
    dp.callback_query.outer_middleware(ThrottleMiddleware())
    dp.callback_query.outer_middleware(errors.ErrorNotifyMiddleware())
    # Стек навигации «Назад»: кладём в конец цепочки, чтобы remember()
    # отработал уже после AccessGate (на заблокированных чатах не пишем)
    # и Throttle (на задудоченных тапах не плодим историю).
    dp.callback_query.outer_middleware(NavStackMiddleware())

    # ВАЖНО: events.router содержит catch-all «menu:*» (menu_any_unhandled),
    # поэтому все роутеры с конкретными menu:-колбэками (в т.ч. settings с
    # «menu:settings») должны быть зарегистрированы ДО него — иначе их
    # перехватывает заглушка и пользователь видит «кнопка устарела».
    dp.include_routers(
        errors.error_router,
        admin.router,
        access_handlers.router,
        # ВАЖНО: settings router раньше start — у неонборднутых пользователей
        # cmd_start в start.py завершается на gate-экране и блокирует pass-
        # хендлеры следующих роутеров. Наш /start-хук (готический шорткат)
        # должен отработать до него; для стандартной темы он делает return и
        # start.py обрабатывает /start как раньше.
        settings.router,
        start.router,
        tracker.router,
        tamagotchi.router,
        games.router,
        shop.router,
        merch.router,
        manual.router,          # 📖 гид + ⚙️ баланс — до catch-all «menu:*» из events
        social.router,
        arena.router,
        stats.router,
        events.router,
    )
    dp.errors.register(errors.on_error)

    scheduler = build_scheduler(bot)
    scheduler.start()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    @dp.startup()
    async def _startup() -> None:
        await on_startup(bot)
        with contextlib.suppress(Exception):
            scheduler.start()
        with contextlib.suppress(Exception):
            from app.services.userbot import start_userbot
            await start_userbot(bot)
        with contextlib.suppress(Exception):
            from app.services.mtproto_reactions import start_reaction_listener
            await start_reaction_listener()

    @dp.shutdown()
    async def _shutdown() -> None:
        scheduler.shutdown(wait=False)
        with contextlib.suppress(Exception):
            from app.services.mtproto_reactions import stop_reaction_listener
            await stop_reaction_listener()
        with contextlib.suppress(Exception):
            from app.services.userbot import stop_userbot
            await stop_userbot()
        await close_redis()
        await engine.dispose()
        await bot.session.close()
        logger.info("👋 graceful shutdown complete")

    try:
        if settings.is_dev or not settings.webhook_url:
            logger.info("starting long polling…")
            await dp.start_polling(
                bot,
                timeout=settings.polling_timeout,
                
                allowed_updates=dp.resolve_used_update_types() + ["message_reaction", "message_reaction_count", "chat_member"],
                handle_signals=False,
            )
            stop.set()
        else:
            from aiogram.webhook.aiohttp_server import SimpleRequestHandler
            from aiohttp import web

            await bot.set_webhook(
                url=f"{settings.webhook_url}/webhook",
                secret_token=settings.webhook_secret_token,
                allowed_updates=dp.resolve_used_update_types() + ["message_reaction", "message_reaction_count", "chat_member"],
            )
            app = web.Application()
            app.router.add_route("POST", "/webhook",
                                 SimpleRequestHandler(dp, bot))
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, port=settings.webhook_port)
            await site.start()
            logger.info("webhook listening on :{}", settings.webhook_port)

        await stop.wait()
    finally:
        if not dp.frozen:
            with contextlib.suppress(Exception):
                await dp.emit_shutdown()

if __name__ == "__main__":
    with contextlib.suppress(Exception):
        asyncio.run(main())
