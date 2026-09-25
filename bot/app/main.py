"""Точка входа: логирование, регистрация роутеров, поллинг/вебхук, graceful shutdown."""
from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand
from loguru import logger

from app.config import get_settings
from app.db.models import Base
from app.db.session import DbMiddleware, engine, session_factory
from app.handlers import (games, settings, shop, social, start, stats,
                         tamagotchi, tracker, welcome)
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


async def ensure_utf8mb4(conn) -> None:
    """Гарантирует полную поддержку эмодзи в MySQL.

    Даже если в DATABASE_URL указан charset=utf8mb4, база на сервере могла
    быть создана с дефолтным utf8mb3 (MySQL 5.x / старый my.cnf) — тогда
    вставка 4-байтных символов (🐾💬🔥) падает с ошибкой 1366.
    Команды конвертации идемпотентны и безопасны (sqlite пропускается).
    """
    if engine.dialect.name != "mysql":
        return
    from sqlalchemy import text

    row = (await conn.execute(text(
        "SELECT @@character_set_database AS cs, @@collation_database AS col"
    ))).mappings().first()
    if row and str(row["cs"]).lower() != "utf8mb4":
        await conn.execute(text(
            "ALTER DATABASE CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        ))
        logger.warning(
            f"база была {row['cs']} — сконвертирована в utf8mb4 (эмодзи OK)")
    # конвертация существующих таблиц/колонок (для старых дампов utf8mb3)
    tables = (await conn.execute(text("SHOW TABLES"))).scalars().all()
    for t in tables:
        await conn.execute(text(
            f"ALTER TABLE `{t}` CONVERT TO CHARACTER SET utf8mb4 "
            "COLLATE utf8mb4_unicode_ci"
        ))


def _make_fsm_storage(redis_url: str):
    """Redis-backed FSM storage.

    ВАЖНО: protocol=2 отключает RESP3-рукопожатие HELLO, которое не
    понимают Redis 2.x/3.x и старые сборки Memurai (<6.0) — иначе первый
    же get_state() падает с «unknown command 'HELLO'». Вызывать только
    после успешного пинга (см. main()).
    """
    from redis.asyncio import Redis

    return RedisStorage(
        Redis.from_url(redis_url, protocol=2),
        key_builder=DefaultKeyBuilder(with_bot_id=True, with_destiny=True),
    )


async def on_startup(bot: Bot) -> None:
    # схемы (в проде — Alembic; create_all оставлен для dev-скорости)
    async with engine.begin() as conn:
        await ensure_utf8mb4(conn)
        await conn.run_sync(Base.metadata.create_all)
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
        BotCommand(command="card", description="🖼 Карточка профиля"),
        BotCommand(command="award", description="🎁 Итоги недели"),
        BotCommand(command="settings", description="⚙️ Настройки"),
    ])
    me = await bot.get_me()
    logger.info(f"подключено к @{me.username} (id={me.id})")
    logger.info("✅ bot started")


async def main() -> None:
    settings = get_settings()
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN не задан — скопируйте .env.example в .env")
    setup_logging(settings.log_level)

    # активная проверка Redis: коннект ленив и падает позже внутри диспетчера,
    # поэтому пингуем сразу — при отказе переключаемся на in-memory хранилище
    redis_ok = False
    try:
        from redis.asyncio import Redis as _ARedis
        # socket_connect/sock_timeout — чтобы недоступный сервер (firewall,
        # зависший Memurai) не блокировал старт бота на таймаут ОС в 20+ сек
        _probe = _ARedis.from_url(settings.redis_url, protocol=2,
                                  socket_connect_timeout=2, socket_timeout=2)
        await asyncio.wait_for(_probe.ping(), timeout=3)
        await _probe.aclose()
        redis_ok = True
    except Exception as exc:
        logger.warning(f"Redis недоступен ({exc!r}) — кулдауны и FSM in-memory "
                       f"(сброс при рестарте; для прод-стабильности поставьте Memurai)")

    init_redis()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    if redis_ok:
        storage = _make_fsm_storage(settings.redis_url)
    else:
        storage = MemoryStorage()

    dp = Dispatcher(storage=storage)
    # мидлвары: сессия БД — глобально, throttle — только на callbacks
    dp.update.outer_middleware(DbMiddleware())
    dp.callback_query.outer_middleware(ThrottleMiddleware())

    @dp.errors()
    async def _on_error(exc_type, exc_value, traceback, event_handler, **kwargs):
        """Сетевые сбои Redis не должны ронять обработку апдейта."""
        import redis.exceptions as _rerr
        if isinstance(exc_value, (_rerr.ResponseError, _rerr.ConnectionError,
                                  _rerr.TimeoutError)):
            logger.warning(f"Redis сбой при обработке апдейта: {exc_value!r} "
                           f"(апдейт пропущен, бот продолжает работу)")
            return True
        return False

    dp.include_routers(
        start.router,
        welcome.router,
        tracker.router,
        tamagotchi.router,
        games.router,
        shop.router,
        social.router,
        stats.router,
        settings.router,
    )

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
            # aiogram при сетевых проблемах ретраит getUpdates молча —
            # включаем DEBUG для aiohttp/aiogram, чтобы такие ситуации было видно
            import logging as _logging
            _logging.getLogger("aiogram").setLevel(_logging.DEBUG)
            await dp.start_polling(
                bot,
                timeout=settings.polling_timeout,
                
                # реакции приходят только если разрешены явно + бот админ с правом реакций
                allowed_updates=dp.resolve_used_update_types() + ["message_reaction"],
                handle_signals=False,
            )
        else:
            from aiogram.webhook.aiohttp_server import SimpleRequestHandler
            from aiohttp import web

            await bot.set_webhook(
                url=f"{settings.webhook_url}/webhook",
                secret_token="change-me-in-env",
                allowed_updates=dp.resolve_used_update_types() + ["message_reaction"],
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
        await dp.emit_shutdown() if not dp.frozen else None


if __name__ == "__main__":
    with contextlib.suppress(Exception):
        asyncio.run(main())
