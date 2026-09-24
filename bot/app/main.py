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


async def on_startup(bot: Bot) -> None:
    # схемы (в проде — Alembic; create_all оставлен для dev-скорости)
    async with engine.begin() as conn:
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
    try:
        storage = RedisStorage.from_url(settings.redis_url)
    except Exception:
        logger.warning("Redis недоступен для FSM — использую MemoryStorage (только dev!)")
        from aiogram.fsm.storage.memory import MemoryStorage
        storage = MemoryStorage()

    dp = Dispatcher(storage=storage)
    # мидлвары: сессия БД — глобально, throttle — только на callbacks
    dp.update.outer_middleware(DbMiddleware())
    dp.callback_query.outer_middleware(ThrottleMiddleware())

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
            await dp.start_polling(
                bot,
                timeout=settings.polling_timeout,
                
                # реакции приходят только если разрешены явно + бот админ с правом реакций
                allowed_updates=dp.resolve_used_update_types() + ["message_reaction"],
                handle_signals=False,
            )
        else:
            from aiogram.webhook.aiohttp_server import SimpleRequestAiohttpHandler, aiohttp_webserver
            from aiohttp import web

            await bot.set_webhook(
                url=f"{settings.webhook_url}/webhook",
                secret_token="change-me-in-env",
                allowed_updates=dp.resolve_used_update_types() + ["message_reaction"],
            )
            app = web.Application()
            app.router.add_route("POST", "/webhook",
                                 SimpleRequestAiohttpHandler(bot.process_update, dp, app))
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
