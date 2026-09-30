from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

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
