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
from app.handlers import (arena, errors, games, merch, settings, shop, social,
                          start, stats, tamagotchi, tracker, welcome)
from app.middlewares.user_lang import UserLanguageMiddleware
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
    dp.update.outer_middleware(UserLanguageMiddleware())  # i18n: язык юзера в contextvars
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
    # aiogram больше не будет логировать её как «Unhandled exceptions».
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
                allowed_updates=dp.resolve_used_update_types() + ["message_reaction", "message_reaction_count"],
                handle_signals=False,
            )
            # polling завершился сам (например, остановлен извне) —
            # переводим процесс в штатную финализацию через finally
            stop.set()
        else:
            # aiogram 3.x: обработчик называется SimpleRequestHandler
            # (имени SimpleRequestAiohttpHandler в библиотеке нет — старый код
            # падал с ImportError при первом же включении вебхука).
            from aiogram.webhook.aiohttp_server import SimpleRequestHandler
            from aiohttp import web

            await bot.set_webhook(
                url=f"{settings.webhook_url}/webhook",
                secret_token=settings.webhook_secret_token,
                allowed_updates=dp.resolve_used_update_types() + ["message_reaction", "message_reaction_count"],
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
        # закроет scheduler/Redis/engine/session. Условие `not dp.frozen` и
        # конструктора-заглушки aiohttp_webserver раньше были мёртвым/битым кодом.
        if not dp.frozen:
            with contextlib.suppress(Exception):
                await dp.emit_shutdown()


if __name__ == "__main__":
    with contextlib.suppress(Exception):
        asyncio.run(main())
