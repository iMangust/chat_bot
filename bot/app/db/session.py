from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

from aiogram.types import CallbackQuery, Message

from app.config import get_settings

_settings = get_settings()

_engine_kwargs: dict = {"echo": False, "pool_pre_ping": True}
if not _settings.database_url.startswith("sqlite"):
    _engine_kwargs.update(pool_size=5, max_overflow=10)

engine = create_async_engine(_settings.database_url, **_engine_kwargs)

session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

class DbMiddleware:

    async def __call__(self, handler, event, data):
        # КЛЮЧЕВОЙ ФИКС «мёртвых» кнопок меню (в т.ч. «Мероприятия»).
        # В aiogram 3.x объекты Message/CallbackQuery из апдейта работают
        # через Bot только если они «смонтированы» на конкретный экземпляр
        # (методы edit_text()/answer() падают с RuntimeError «This method is
        # not mounted to a any bot instance», если этого не сделано).
        # Монтируем здесь один раз для всех хендлеров.
        bot = data.get("bot")
        if bot is not None:
            # ВАЖНО: модели aiogram 3.x frozen — переприсваивать атрибуты
            # апдейта (event.message / cb.message) нельзя; но приватный слот
            # _bot у pydantic-объекта открыт для записи. as_(bot) возвращает
            # копию, которая до хендлера не доходит, поэтому монтируем бота
            # прямо в существующие объекты — так edit_text()/answer() внутри
            # всех хендлеров работают без RuntimeError «not mounted».
            def _mount(obj) -> None:
                if isinstance(obj, Message):
                    obj._bot = bot
                elif isinstance(obj, CallbackQuery):
                    if obj.message is not None:
                        obj.message._bot = bot

            for key in ("message", "edited_message", "channel_post",
                        "edited_channel_post", "callback_query",
                        "inline_query", "chosen_inline_result"):
                _mount(getattr(event, key, None))

        # Тема оформления: кладём session-зависимое чтение в data, чтобы
        # ThemeMiddleware (следующий слой) получил сессию через data["session"]
        # и не открывал отдельное соединение. Чтение идемпотентно; кэш на
        # процесс — в app/themes.py (load_theme_key).
        async with session_factory() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            try:
                await session.commit()
            except Exception:
                # Если commit упал (например, IntegrityError из-за гонки),
                # обязательно откатываем транзакцию, иначе сессия остаётся в
                # состоянии "aborted" и последующие запросы/логи дают каскад
                # OperationalError.
                await session.rollback()
                raise
            return result
