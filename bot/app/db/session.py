"""Асинхронный слой доступа к БД: engine, session factory, мидлвар для хендлеров."""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

from app.config import get_settings

_settings = get_settings()

# pool_* параметры валидны только для серверных движков (Postgres/MySQL);
# для sqlite (dev/тесты) создаём engine без них.
_engine_kwargs: dict = {"echo": False, "pool_pre_ping": True}
if not _settings.database_url.startswith("sqlite"):
    _engine_kwargs.update(pool_size=5, max_overflow=10)

engine = create_async_engine(_settings.database_url, **_engine_kwargs)

session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Зависимость для FastAPI/aiogram: коммит при успехе, откат при ошибке."""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


class DbMiddleware:
    """Aiogram-мидлвар: кладёт активную сессию БД в data['session'].

    Использование в хендлере:
        async def handler(message: Message, session: AsyncSession): ...
    """

    async def __call__(self, handler, event, data):
        async with session_factory() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise
