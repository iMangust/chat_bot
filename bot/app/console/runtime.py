from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections import deque
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.state import State
from aiogram.types import BotCommand, BotCommandScopeAllChatAdministrators, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
from loguru import logger

from app.config import get_settings
from app.db.models import Base
from app.db.session import DbMiddleware, engine, session_factory
from app.handlers import (access as access_handlers, admin, arena, errors,
                          events, games, merch,
                          settings as settings_handlers, shop, social, start,
                          stats, tamagotchi, tracker)
from app.handlers.shop import seed_items
from app.middlewares.throttle import ThrottleMiddleware
from app.services.achievements import seed_achievements
from app.tasks.scheduler import build_scheduler
from app.utils.redis import close_redis, init_redis

class UiLogHandler:

    def __init__(self, maxlen: int = 1000) -> None:
        self.buffer: deque[str] = deque(maxlen=maxlen)
        self._callback = None

    def set_callback(self, callback) -> None:
        self._callback = callback

    def __call__(self, message) -> None:
        record = message.record
        line = (f"{record['time']:HH:mm:ss} | {record['level'].name:<7} | "
                f"{record['name']} - {record['message']}")
        self.buffer.append(line)
        cb = self._callback
        if cb is not None:
            try:
                cb(line)
            except Exception:
                pass

ui_log_handler = UiLogHandler()

def _detach_router(router: Dispatcher) -> None:
    stack = [router]
    while stack:
        node = stack.pop()
        for sub in list(getattr(node, "sub_routers", ())):
            stack.append(sub)
            with contextlib.suppress(Exception):
                node.sub_routers.remove(sub)
            with contextlib.suppress(Exception):
                sub._parent_router = None

def _reset_router_state(dp: Dispatcher) -> None:
    _detach_router(dp)
    with contextlib.suppress(Exception):
        dp.resolve_used_update_types()
    for chain_name in ("update", "errors"):
        chain = getattr(dp, chain_name, None)
        if chain is not None:
            with contextlib.suppress(Exception):
                chain.unresolvable_handlers.clear()
    for cls in getattr(State, "__subclasses__", lambda: [])():
        for name, val in list(vars(cls).items()):
            if isinstance(val, dict):
                for f in val.values():
                    with contextlib.suppress(Exception):
                        if getattr(f, "_dispatcher", None) is dp:
                            f._dispatcher = None

class BotRuntime:

    def __init__(self) -> None:
        self.state: str = "stopped"
        self.started_at: float | None = None
        self.bot: Bot | None = None
        self.dp: Dispatcher | None = None
        self._scheduler = None
        self._polling_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.last_error: str | None = None

    async def start(self) -> None:
        if self.state != "stopped":
            raise RuntimeError(f"бот уже в состоянии {self.state!r}")
        settings = get_settings()
        token = os.environ.get("TAMABOT_TOKEN_OVERRIDE") or settings.bot_token
        if not token or token == "test":
            raise RuntimeError("BOT_TOKEN не задан — проверьте .env или настройки GUI-оболочки")

        self.state = "starting"
        self.last_error = None
        self._loop = asyncio.get_running_loop()
        try:
            init_redis()

            self.bot = Bot(
                token=token,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
            from app.main import _make_fsm_storage, probe_fsm_storage
            storage = _make_fsm_storage(settings.redis_url)
            storage = await probe_fsm_storage(storage)

            dp = Dispatcher(storage=storage)
            dp.update.outer_middleware(DbMiddleware())
            from app.middlewares.theme import (ThemeMiddleware,
                                               ThemeErrorMiddleware,
                                               ThemeGuardMiddleware)
            dp.update.outer_middleware(ThemeMiddleware())
            dp.errors.middleware(ThemeErrorMiddleware())
            # Inner-гарант темы перед каждым хендлером (см. main.py)
            for _obs in (dp.callback_query, dp.message, dp.edited_message):
                _obs.middleware.register(ThemeGuardMiddleware())
            dp.update.outer_middleware(access_handlers.AccessEventsMiddleware())
            from app.middlewares.gate import AccessGateMiddleware
            dp.update.outer_middleware(AccessGateMiddleware())
            dp.callback_query.outer_middleware(ThrottleMiddleware())
            dp.callback_query.outer_middleware(errors.ErrorNotifyMiddleware())
            # ВАЖНО: events.router содержит catch-all «menu:*» — конкретные
            # menu:-колбэки (settings и др.) регистрируем ДО него.
            dp.include_routers(
                errors.error_router,
                admin.router,
                access_handlers.router,
                # settings раньше start: наш /start-хук для готической темы
                # должен иметь шанс отработать до cmd_start из start.py
                # (для стандартной темы он делает return — поведение не меняется).
                settings_handlers.router,
                start.router, tracker.router,
                tamagotchi.router, games.router, shop.router,
                merch.router,
                social.router, arena.router, stats.router,
                events.router,
            )
            dp.errors.register(errors.on_error)

            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            from app.main import (_backfill_subscriber_chats_v202,
                                  _light_migrations,
                                  _migrate_channel_subscribers_v20,
                                  ensure_events_table)
            # Тот же набор миграций/само-исцелений схемы, что и в app.main.on_startup:
            # без него лёгкие колонки (welcome_shown и др.) и таблица events не
            # создаются при запуске через веб-панель.
            async with engine.begin() as conn:
                await _light_migrations(conn)
            await ensure_events_table(engine)
            await _migrate_channel_subscribers_v20(engine)
            await _backfill_subscriber_chats_v202(engine)
            async with session_factory() as session:
                await seed_achievements(session)
                await seed_items(session)
                await session.commit()

            await self.bot.delete_my_commands()
            await self.bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
            await self.bot.delete_my_commands(scope=BotCommandScopeAllChatAdministrators())
            await self.bot.set_my_commands(
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
                scope=BotCommandScopeAllPrivateChats(),
            )

            me = await self.bot.get_me()
            logger.info(f"подключено к @{me.username} (id={me.id})")

            self._scheduler = build_scheduler(self.bot)
            self._scheduler.start()
            self.dp = dp
            self.started_at = time.time()
            self.state = "running"

            allowed = dp.resolve_used_update_types() + ["message_reaction", "message_reaction_count", "chat_member"]

            @dp.startup()
            async def _startup_extras() -> None:
                with contextlib.suppress(Exception):
                    from app.services.userbot import start_userbot
                    await start_userbot(self.bot)
                with contextlib.suppress(Exception):
                    from app.services.mtproto_reactions import start_reaction_listener
                    await start_reaction_listener()

            @dp.shutdown()
            async def _shutdown_extras() -> None:
                with contextlib.suppress(Exception):
                    from app.services.mtproto_reactions import stop_reaction_listener
                    await stop_reaction_listener()
                with contextlib.suppress(Exception):
                    from app.services.userbot import stop_userbot
                    await stop_userbot()

            self._polling_task = asyncio.create_task(
                dp.start_polling(
                    self.bot,
                    timeout=settings.polling_timeout,
                    allowed_updates=allowed,
                    handle_signals=False,
                ),
                name="bot-polling",
            )
            self._polling_task.add_done_callback(self._on_polling_done)
            logger.info("✅ bot started (управляемый запуск из оболочки)")
            logger.info("📡 слушаю обновления (long polling)… напишите боту /start в ЛС")
            import logging as _logging
            _logging.getLogger("aiogram").setLevel(_logging.DEBUG)
        except Exception as exc:
            await self._cleanup_partial()
            self.state = "stopped"
            self.last_error = str(exc)
            raise

    def _on_polling_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and self.state == "running":
            logger.error("💥 polling crashed: {}", exc)
            self.last_error = str(exc)
            if self._loop is not None:
                asyncio.ensure_future(self._cleanup_partial(), loop=self._loop)
            self.state = "stopped"

    async def stop(self) -> None:
        if self.state not in ("running", "starting"):
            return
        self.state = "stopping"
        logger.info("останавливаюсь…")
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None
        if self._polling_task is not None:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except (asyncio.CancelledError, Exception):
                pass
            self._polling_task = None
        await close_redis()
        await engine.dispose()
        if self.bot is not None:
            await self.bot.session.close()
            self.bot = None
        if self.dp is not None:
            _reset_router_state(self.dp)
        self.dp = None
        self.started_at = None
        self.state = "stopped"
        logger.info("👋 bot stopped")

    async def _cleanup_partial(self) -> None:
        try:
            await close_redis()
            await engine.dispose()
        except Exception:
            pass
        if self.bot is not None:
            try:
                await self.bot.session.close()
            except Exception:
                pass
            self.bot = None
        if self.dp is not None:
            _reset_router_state(self.dp)
        self.dp = None

    @property
    def uptime_sec(self) -> float:
        if self.started_at is None:
            return 0.0
        return time.time() - self.started_at

    def jobs_info(self) -> list[tuple[str, str]]:
        if self._scheduler is None or not self._scheduler.running:
            return []
        out = []
        for job in self._scheduler.get_jobs():
            nxt = job.next_run_time
            out.append((job.id, nxt.strftime("%d.%m %H:%M:%S") if nxt else "—"))
        return out

    def reschedule_cron_jobs(self) -> None:
        """Пересоздаёт cron-джобы с текущими часами из Settings (после
        сохранения DAILY_REPORT_HOUR / *_REMINDER_HOUR без рестарта)."""
        if self._scheduler is None or not self._scheduler.running:
            return
        try:
            from app.tasks.scheduler import build_scheduler
            new = build_scheduler(self.bot)
            old = self._scheduler
            new.start()
            self._scheduler = new
            with contextlib.suppress(Exception):
                old.shutdown(wait=False)
        except Exception as exc:
            logger.warning("reschedule_cron_jobs failed: {}: {}",
                           type(exc).__name__, exc)

runtime = BotRuntime()

def setup_file_logging(level: str = "INFO") -> Path:
    logger.remove()
    logger.add(ui_log_handler, level=level)
    Path("logs").mkdir(exist_ok=True)
    logger.add("logs/bot_{time:YYYY-MM-DD}.log", rotation="1 day",
               retention="14 days", level="DEBUG", encoding="utf-8")
    return Path("logs")
