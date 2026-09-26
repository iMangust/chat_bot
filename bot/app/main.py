"""Точка входа: логирование, регистрация роутеров, поллинг/вебхук, graceful shutdown."""
from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand
from loguru import logger

from sqlalchemy import inspect as sa_inspect

from app.config import get_settings
from app.db.models import Base
from app.db.session import DbMiddleware, engine, session_factory
from app.handlers import (arena, errors, games, merch, settings, shop,
                          social, start, stats, tamagotchi, tracker, welcome)
from app.middlewares.gate import AccessGateMiddleware
from app.middlewares.throttle import ThrottleMiddleware
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


def _make_fsm_storage(redis_url: str):
    """FSM-хранилище с совместимостью со старыми Redis (< 6.0).

    redis-py>=5 по умолчанию шлёт команду HELLO (RESP3/протокол 3),
    которую Memurai для Windows и старый Redis не понимают — падение
    с ошибкой \"unknown command 'HELLO'\". Фиксим двумя уровнями:
      1) protocol=2 (RESP2) — стандартный протокол для любого Redis >= 2.6;
      2) graceful fallback на MemoryStorage, если Redis недоступен
         ИЛИ несовместим (проверка PING выполняется сразу, т.к.
         redis-py соединяется лениво и ошибки всплыли бы только при
         первом сообщении пользователя).

    ВАЖНО: RedisStorage принимает именно экземпляр Redis, а не
    ConnectionPool (pool передаётся конструктору Redis).
    """
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
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Redis недоступен для FSM ({exc!r}) — использую MemoryStorage "
                       f"(стейты сбрасываются при рестарте; для dev допустимо)")
        return MemoryStorage()


async def probe_fsm_storage(storage):
    """Активная проверка хранилища FSM (PING).

    Возвращает исходное хранилище, если оно рабочее; иначе —
    MemoryStorage с понятным предупреждением в логе. Вызывается
    ДО старта long polling, чтобы не ловить ошибки на апдейтах.
    """
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    if isinstance(storage, MemoryStorage):
        return storage
    try:
        await storage.redis.ping()
        await storage.get_state(key=StorageKey(bot_id=0, chat_id=0, user_id=0))
        return storage
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"FSM-хранилище (Redis) проверено и отключено ({type(exc).__name__}: {exc}) — "
            f"перехожу на MemoryStorage (кулдауны стейтов сбрасываются при рестарте)")
        return MemoryStorage()


async def _column_exists(conn, table: str, column: str) -> bool:
    """Проверка наличия колонки через inspector (кросс-СУБД, без information_schema вручную)."""
    def _check(sync_conn) -> bool:
        try:
            cols = {c["name"] for c in sa_inspect(sync_conn).get_columns(table)}
        except Exception:  # таблицы ещё нет — create_all создаст её со свежей схемой
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


# описание лёгких миграций: таблица → [(колонка, DDL-тип)]
_LIGHT_COLUMNS: dict[str, list[tuple[str, str]]] = {
    # история питомцев: у пользователя может быть несколько строк
    # (архив + текущий), «текущий» выбирается фильтром is_archived=False.
    "pets": [
        ("generation", "INTEGER NOT NULL DEFAULT 1"),
        ("is_archived", "BOOLEAN NOT NULL DEFAULT 0"),
        ("archived_at", "DATETIME"),
        ("archive_reason", "VARCHAR(32)"),
    ],
}


async def _light_migrations(conn) -> None:
    """Лёгкие инкрементальные миграции для колонок, появившихся после v1.4.6.

    create_all умеет только СОЗДАвать недостающие таблицы, но не добавляет
    колонки в уже существующие — на живой БД pets без generation/is_archived
    новый код падал с OperationalError (1054 Unknown column на MySQL).

    Реализация кросс-СУБД (MySQL/MariaDB, PostgreSQL, SQLite): сначала через
    inspector проверяем, чего реально не хватает, затем выполняем обычный
    ``ALTER TABLE ... ADD COLUMN`` БЕЗ ``IF NOT EXISTS`` — этого синтаксиса в
    MySQL нет (он есть только в PG/SQLite 3.35+, но и там предварительная
    проверка делает его избыточным). Ошибки конкретной инструкции логируются
    и не валят старт бота (best-effort; в проде — Alembic).
    """
    from sqlalchemy import text

    dialect = conn.dialect.name  # 'mysql' | 'postgresql' | 'sqlite' | ...

    for table, columns in _LIGHT_COLUMNS.items():
        for column, ddl_type in columns:
            if not await _column_exists(conn, table, column):
                sql = f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"
                try:
                    await conn.execute(text(sql))
                    logger.info("лёгкая миграция: {}.{} добавлена ({})", table, column, dialect)
                except Exception as exc:  # noqa: BLE001 — гонка с другим воркером и т.п.
                    logger.warning(
                        "лёгкая миграция {} не применена ({}): {}",
                        sql[:80], type(exc).__name__, exc,
                    )

    # Защита «не более одного текущего питомца на пользователя» — частичный
    # уникальный индекс. Частичные индексы (WHERE) MySQL не поддерживает:
    # на MySQL/MariaDB целостность обеспечивает фильтр is_archived=False во
    # всех запросах + проверка в сервисе усыновления.
    idx_name = "uq_pets_current_per_user"
    if dialect in {"postgresql", "sqlite"} and not await _index_exists(conn, idx_name):
        try:
            await conn.execute(text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {idx_name} "
                "ON pets (user_id) WHERE COALESCE(is_archived, 0) = 0"
            ))
        except Exception as exc:  # noqa: BLE001 — старая sqlite без partial-индексов и т.п.
            logger.debug("частичный индекс {} пропущен: {}", idx_name, type(exc).__name__)


async def on_startup(bot: Bot) -> None:
    # схемы (в проде — Alembic; create_all оставлен для dev-скорости)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _light_migrations(conn)
    async with session_factory() as session:
        await seed_achievements(session)
        await seed_items(session)   # справочник магазина (идемпотентно)
        await session.commit()
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="pet", description="🐾 Питомец"),
        BotCommand(command="stats", description="📊 Моя статистика"),
        BotCommand(command="ach", description="🏆 Достижения"),
        BotCommand(command="top", description="🏅 Топы"),
        BotCommand(command="arena", description="⚔️ Арена питомцев"),
        BotCommand(command="card", description="🖼 Карточка профиля"),
        BotCommand(command="award", description="🎁 Итоги недели"),
        BotCommand(command="settings", description="⚙️ Настройки"),
        BotCommand(command="help", description="❓ Справка"),
    ])
    logger.info("✅ bot started")


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
    storage = _make_fsm_storage(settings.redis_url)
    storage = await probe_fsm_storage(storage)  # PING до поллинга: несовместимый Redis -> MemoryStorage

    dp = Dispatcher(storage=storage)
    # мидлвары: сессия БД — глобально, throttle — только на callbacks
    dp.update.outer_middleware(DbMiddleware())
    # Глобальный доступ: только ЛС + только подписчики канала (v1.5.3).
    # Правило «бот молчит в группах/каналах» и проверка подписки — здесь;
    # пассивный трекер активности (tracker.py) работает поверх этого правила,
    # потому что регистрируется отдельным фильтром по chat.type.
    dp.update.outer_middleware(AccessGateMiddleware())
    dp.callback_query.outer_middleware(ThrottleMiddleware())
    # страховка на уровне callback-мидлваров (ошибки до/вне хендлеров:
    # throttle, FSM, БД-сессия) — пользователь получит тост, а не «вечные часы»
    dp.callback_query.outer_middleware(errors.ErrorNotifyMiddleware())

    dp.include_routers(
        errors.error_router,   # страховка: падающий хендлер не «вешает» callback
        start.router,
        welcome.router,
        tracker.router,
        tamagotchi.router,
        games.router,
        shop.router,
        merch.router,
        social.router,
        arena.router,
        stats.router,
        settings.router,
    )
    # Глобальная страховка уровня диспетчера: даже если ошибка возникнет вне
    # error_router (например, в другом мидлваре), обработчик на месте —
    # aiogram не будет логировать её как «Unhandled exceptions».
    dp.errors.register(errors.on_error)

    scheduler = build_scheduler(bot)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    @dp.startup()
    async def _startup() -> None:
        await on_startup(bot)
        scheduler.start()

    @dp.shutdown()
    async def _shutdown() -> None:
        scheduler.shutdown(wait=False)
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
                
                # реакции приходят только если разрешены явно + бот админ с правом реакций
                allowed_updates=dp.resolve_used_update_types() + ["message_reaction", "message_reaction_count", "chat_member"],
                handle_signals=False,
            )
            # polling завершился сам (например, остановлен извне) —
            # переводим процесс в штатную финализацию через finally
            stop.set()
        else:
            # aiogram 3.x: обработчик называется SimpleRequestHandler
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

        await stop.wait()  # ждём сигнал завершения
    finally:
        # Штатный graceful shutdown: эмитим событие shutdown — хендлер _shutdown
        # закроет scheduler/Redis/engine/session.
        if not dp.frozen:
            with contextlib.suppress(Exception):
                await dp.emit_shutdown()


if __name__ == "__main__":
    with contextlib.suppress(Exception):
        asyncio.run(main())
