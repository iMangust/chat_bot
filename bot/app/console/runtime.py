"""Управляемая из GUI версия жизненного цикла бота.

Оболочка (app/console) запускает бота внутри своего asyncio-цикла событий,
поэтому сигнальные обработчики не нужны, а статус/логи должны быть доступны
из UI-потока. Модуль предоставляет:

* :class:`BotRuntime` — запуск/остановка aiogram-бота + планировщика;
* :class:`UiLogHandler` — sink loguru, который буферизует лог-записи и
  опционально транслирует их в UI через колбэк;
* глобальный singleton-экземпляр runtime (для панели статуса и вкладок).
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from loguru import logger

from app.config import get_settings
from app.db.models import Base
from app.db.session import DbMiddleware, engine, session_factory
from app.handlers import (arena, errors, games, merch,
                          settings as settings_handlers, shop, social, start,
                          stats, tamagotchi, tracker, welcome)
from app.handlers.shop import seed_items
from app.middlewares.throttle import ThrottleMiddleware
from app.services.achievements import seed_achievements
from app.tasks.scheduler import build_scheduler
from app.utils.redis import close_redis, init_redis


class UiLogHandler:
    """Sink loguru: буфер последних строк + колбэк в UI.

    Колбэк вызывается синхронно из того же потока, что пишет лог (то есть из
    event loop'а оболочки), поэтому Textual может принять строку через
    ``call_from_thread`` внутри своего колбэка.
    """

    def __init__(self, maxlen: int = 1000) -> None:
        self.buffer: deque[str] = deque(maxlen=maxlen)
        self._callback = None  # callable(str) -> None; ставится из UI

    def set_callback(self, callback) -> None:
        self._callback = callback

    def __call__(self, message) -> None:  # loguru sink protocol
        record = message.record
        line = (f"{record['time']:HH:mm:ss} | {record['level'].name:<7} | "
                f"{record['name']} - {record['message']}")
        self.buffer.append(line)
        cb = self._callback
        if cb is not None:
            try:
                cb(line)
            except Exception:  # noqa: BLE001 — логгер не должен ронять UI
                pass


ui_log_handler = UiLogHandler()


class BotRuntime:
    """Запуск и остановка бота по требованию (singleton — :data:`runtime`)."""

    def __init__(self) -> None:
        self.state: str = "stopped"          # stopped | starting | running | stopping
        self.started_at: float | None = None
        self.bot: Bot | None = None
        self.dp: Dispatcher | None = None
        self._scheduler = None
        self._polling_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ run
    async def start(self) -> None:
        """Полный старт: Redis → БД → сиды → роутеры → поллинг."""
        if self.state != "stopped":
            raise RuntimeError(f"бот уже в состоянии {self.state!r}")
        settings = get_settings()
        # при запуске из bat-файла токен можно передать переменной окружения
        token = os.environ.get("TAMABOT_TOKEN_OVERRIDE") or settings.bot_token
        if not token or token == "test":
            raise RuntimeError("BOT_TOKEN не задан — проверьте .env или запустите через run.bat")

        self.state = "starting"
        self.last_error = None
        self._loop = asyncio.get_running_loop()
        try:
            init_redis()

            self.bot = Bot(
                token=token,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
            # ВАЖНО: используем общий конструктор из app.main — он принудительно
            # ставит protocol=2 (RESP2) и оборачивает ConnectionPool в Redis
            # (RedisStorage принимает именно Redis, а не пул). Дополнительно
            # probe_fsm_storage делает PING до поллинга: если Memurai/Redis
            # старый или недоступен — тихо деградируем на MemoryStorage.
            from app.main import _make_fsm_storage, probe_fsm_storage
            storage = _make_fsm_storage(settings.redis_url)
            storage = await probe_fsm_storage(storage)

            dp = Dispatcher(storage=storage)
            dp.update.outer_middleware(DbMiddleware())
            dp.callback_query.outer_middleware(ThrottleMiddleware())
            # страховка на уровне callback-мидлваров (до/вне хендлеров) —
            # юзер не останется с «висящими часами», а админы увидят сбой в логе
            dp.callback_query.outer_middleware(errors.ErrorNotifyMiddleware())
            dp.include_routers(
                errors.error_router,
                start.router, welcome.router, tracker.router,
                tamagotchi.router, games.router, shop.router,
                merch.router,
                social.router, arena.router, stats.router,
                settings_handlers.router,
            )
            dp.errors.register(errors.on_error)

            # схемы + справочники (идемпотентно)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with session_factory() as session:
                await seed_achievements(session)
                await seed_items(session)
                await session.commit()

            await self.bot.set_my_commands([
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

            # проверка токена/связи с Telegram API до старта поллинга —
            # иначе при неверном токене или сетевом проблеме getUpdates просто
            # ретраит молча и выглядит как «завис»
            me = await self.bot.get_me()
            logger.info(f"подключено к @{me.username} (id={me.id})")

            self._scheduler = build_scheduler(self.bot)
            self._scheduler.start()
            self.dp = dp
            self.started_at = time.time()
            self.state = "running"

            allowed = dp.resolve_used_update_types() + ["message_reaction", "message_reaction_count"]
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
            # при сетевых проблемах aiogram ретраит getUpdates молча — включаем
            # DEBUG для aiogram, чтобы такие ситуации было видно в логах
            import logging as _logging
            _logging.getLogger("aiogram").setLevel(_logging.DEBUG)
        except Exception as exc:  # noqa: BLE001
            await self._cleanup_partial()
            self.state = "stopped"
            self.last_error = str(exc)
            raise

    def _on_polling_done(self, task: asyncio.Task) -> None:
        """Если поллинг упал сам — фиксируем ошибку и чиним состояние."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and self.state == "running":
            logger.error("💥 polling crashed: {}", exc)
            self.last_error = str(exc)
            if self._loop is not None:
                asyncio.ensure_future(self._cleanup_partial(), loop=self._loop)
            self.state = "stopped"

    # ---------------------------------------------------------------- stop
    async def stop(self) -> None:
        """Graceful shutdown: планировщик → поллинг → Redis/engine/session."""
        if self.state not in ("running", "starting"):
            return
        self.state = "stopping"
        logger.info("останавливаюсь…")
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
            self._scheduler = None
        if self._polling_task is not None:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._polling_task = None
        await close_redis()
        await engine.dispose()
        if self.bot is not None:
            await self.bot.session.close()
            self.bot = None
        self.dp = None
        self.started_at = None
        self.state = "stopped"
        logger.info("👋 bot stopped")

    async def _cleanup_partial(self) -> None:
        try:
            await close_redis()
            await engine.dispose()
        except Exception:  # noqa: BLE001
            pass
        if self.bot is not None:
            try:
                await self.bot.session.close()
            except Exception:  # noqa: BLE001
                pass
            self.bot = None
        self.dp = None

    # -------------------------------------------------------------- status
    @property
    def uptime_sec(self) -> float:
        if self.started_at is None:
            return 0.0
        return time.time() - self.started_at

    def jobs_info(self) -> list[tuple[str, str]]:
        """[(id, следующее срабатывание)] активных задач планировщика."""
        if self._scheduler is None or not self._scheduler.running:
            return []
        out = []
        for job in self._scheduler.get_jobs():
            nxt = job.next_run_time
            out.append((job.id, nxt.strftime("%d.%m %H:%M:%S") if nxt else "—"))
        return out


# Глобальный экземпляр для GUI (один процесс — один бот).
runtime = BotRuntime()


def setup_file_logging(level: str = "INFO") -> Path:
    """Настроить loguru: консоль подавляется (её перехватывает оболочка),
    файл логов остаётся обязательным."""
    logger.remove()
    logger.add(ui_log_handler, level=level)
    Path("logs").mkdir(exist_ok=True)
    logger.add("logs/bot_{time:YYYY-MM-DD}.log", rotation="1 day",
               retention="14 days", level="DEBUG", encoding="utf-8")
    return Path("logs")
