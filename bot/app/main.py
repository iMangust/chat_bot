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
from aiogram.types import BotCommand, BotCommandScopeAllChatAdministrators, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
from loguru import logger

from sqlalchemy import inspect as sa_inspect

from app.config import get_settings
from app.db.models import Base
from app.db.session import DbMiddleware, engine, session_factory
from app.handlers import (admin, arena, errors, games, merch, shop, social,
                          start, stats, tamagotchi, tracker, welcome)
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
    # v1.5.10: по какому чату отправлено приветствие — защита от двойных
    # welcome-DM для участников канала И группы одновременно
    "channel_subscribers": [
        ("welcome_sent_chat_id", "BIGINT"),
    ],
}


async def _migrate_channel_subscribers_pk(engine) -> None:
    """PK channel_subscribers: user_id → (user_id, chat_id).

    Идемпотентно: если PK уже составной (в SQLite это видно по sql CREATE
    TABLE, в MySQL/PG — по количеству колонок в pk), ничего не делаем.
    Старая таблица с PK(user_id) конвертируется ALTER-ом (MySQL/SQLite 3.25+:
    DROP PRIMARY KEY / без него — fallback на пересоздание через временную
    таблицу). Данные сохраняются; приветствованные остаются приветствованными.
    """
    from sqlalchemy import text
    async with engine.begin() as conn:
        dialect = conn.dialect.name
        try:
            if dialect == "sqlite":
                row = (await conn.execute(text(
                    "SELECT sql FROM sqlite_master WHERE type='table' "
                    "AND name='channel_subscribers'"))).first()
                ddl = (row[0] or "").lower() if row else ""
                if "primary key (user_id, chat_id)" in ddl.replace(" ", "").replace(
                        "primarykey(user_id,chat_id)", "primary key (user_id, chat_id)"):
                    return  # уже мигрировано
                await conn.execute(text("PRAGMA foreign_keys=OFF"))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS channel_subscribers_new (
                        user_id BIGINT NOT NULL,
                        chat_id BIGINT NOT NULL DEFAULT 0,
                        username VARCHAR(64),
                        first_name VARCHAR(128) NOT NULL DEFAULT '',
                        first_seen DATETIME NOT NULL,
                        welcomed_at DATETIME,
                        welcome_sent_chat_id BIGINT,
                        PRIMARY KEY (user_id, chat_id)
                    )"""))
                await conn.execute(text("""
                    INSERT OR IGNORE INTO channel_subscribers_new
                    SELECT user_id, chat_id, username, first_name, first_seen,
                           welcomed_at, welcome_sent_chat_id
                    FROM channel_subscribers"""))
                await conn.execute(text("DROP TABLE channel_subscribers"))
                await conn.execute(text(
                    "ALTER TABLE channel_subscribers_new RENAME TO channel_subscribers"))
                await conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_channel_subscribers_first_seen "
                    "ON channel_subscribers (first_seen)"))
                logger.info("миграция v1.5.12: PK channel_subscribers → (user_id, chat_id)")
            else:
                cols = (await conn.execute(text(
                    "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                    "WHERE TABLE_NAME='channel_subscribers' AND CONSTRAINT_NAME='PRIMARY' "
                    f"AND TABLE_SCHEMA={'DATABASE()' if dialect == 'mysql' else 'current_schema()'}"
                ))).scalars().all()
                if len(cols) >= 2:
                    return  # уже составной PK
                if dialect == "mysql":
                    await conn.execute(text(
                        "ALTER TABLE channel_subscribers DROP PRIMARY KEY, "
                        "ADD PRIMARY KEY (user_id, chat_id)"))
                else:  # postgresql
                    await conn.execute(text(
                        "ALTER TABLE channel_subscribers DROP CONSTRAINT channel_subscribers_pkey, "
                        "ADD PRIMARY KEY (user_id, chat_id)"))
                logger.info("миграция v1.5.12: PK channel_subscribers → (user_id, chat_id)")
        except Exception as exc:  # noqa: BLE001 — повторная миграция безопасна
            logger.warning("миграция PK channel_subscribers пропущена: {}: {}",
                           type(exc).__name__, str(exc)[:200])


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
    # v1.5.12: PK channel_subscribers (user_id) → (user_id, chat_id):
    # дедупликация welcome-очереди по паре «пользователь+чат» — MTProto-sync
    # больше не может пересоздать pending уже зарегистрированному подписчику.
    await _migrate_channel_subscribers_pk(engine)
    async with session_factory() as session:
        await seed_achievements(session)
        await seed_items(session)   # справочник магазина (идемпотентно)
        await session.commit()
    # Меню команд ("/...") регистрируем ТОЛЬКО для личных чатов.
    # В группах и каналах взаимодействие с ботом запрещено моделью доступа
    # (AccessGateMiddleware), поэтому список команд там вводил в заблуждение.
    # setMyCommands без scope не сбрасывает скоупы — сначала очищаем всё,
    # иначе старые глобальные команды продолжат отображаться в группах.
    await bot.delete_my_commands()          # глобальный scope
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
            BotCommand(command="settings", description="⚙️ Настройки"),
            BotCommand(command="help", description="❓ Справка"),
        ],
        scope=private_scope,
    )
    logger.info("✅ bot started")
    # v1.5.12: если настроен полный Telegram API (Telethon) — тянем список
    # участников канала ЦЕЛИКОМ (Bot API его не отдаёт). Первый прогон —
    # полная синхронизация (старые подписки тоже получат приветствие),
    # дальше дельта по cron в scheduler. Ошибки MTProto не валят старт бота.
    try:
        from app.services.mtproto_sync import autosync_if_configured
        st = get_settings()
        if st.mtproto_autosync:
            res = await autosync_if_configured()
            if res is not None:
                logger.info("🔄 MTProto autosync: {}", res)
        else:
            asyncio.create_task(autosync_guard())
    except Exception as exc:  # noqa: BLE001 — без telethon/кредов бот живёт на Bot API
        logger.info("MTProto autosync недоступен ({}) — работаю только на Bot API",
                    type(exc).__name__)
    # после рестарта догоняем неотправленные приветствия подписчикам канала
    try:
        from app.handlers.welcome import welcome_pending_subscribers
        async with session_factory() as session:
            sent = await welcome_pending_subscribers(bot, session, limit=10)
        if sent:
            logger.info("👋 startup catch-up welcomed {} subscriber(s)", sent)
    except Exception as exc:  # noqa: BLE001 — старт не должен падать из-за приветствий
        logger.warning("startup welcome catch-up failed: {}", exc)


async def autosync_guard() -> None:
    """Фоновый запасной автосинк (если on_startup пропустил запуск)."""
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
        admin.router,          # /mtproto, /syncnow — только ADMIN_IDS (проверка внутри)
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
    # v1.5.16: планировщик стартует СРАЗУ (раньше — только внутри on_startup,
    # который aiogram вызывает уже ВНУТРИ start_polling; из-за этого первая
    # MTProto-дельта падала с «Bot is not initialized», т.к. session.start()
    # ещё не выполнился к моменту add_job(next_run_time=+90s)).
    scheduler.start()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    @dp.startup()
    async def _startup() -> None:
        await on_startup(bot)
        # scheduler.start() идемпотентен; вызываем ещё раз на случай, если
        # main-путь (webhook) пойдёт в обход блока выше
        with contextlib.suppress(Exception):
            scheduler.start()
        # v1.5.12: UserBot (полный API, режимы user/hybrid) — ошибки не валят бота
        with contextlib.suppress(Exception):
            from app.services.userbot import start_userbot
            await start_userbot(bot)
        # v1.5.19: MTProto reaction listener — Bot API НЕ отдаёт боту реакции
        # на чужие сообщения (посты канала, сообщения других юзеров). User-
        # аккаунт в тех же чатах видит их сырыми TL-update'ами и засчитывает
        # через общую логику process_reaction. Без ключей MTProto — no-op.
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
