"""MTProto-синхронизация подписчиков (v1.5.11)."""
from __future__ import annotations

from _helpers import _coro

# ---------------------------------------------------------------------------
# v1.5.11: MTProto-синхронизация подписчиков (опциональный Telegram API)
# ---------------------------------------------------------------------------
class TestMtprotoSync:
    async def test_last_seen_user_id_cursor(self, session):
        """Курсор «новых» = max(user_id); пустая база -> None."""
        from app.db.repositories import SubscriberRepository
        subs = SubscriberRepository(session)
        assert await subs.last_seen_user_id() is None
        await subs.add_if_new(500, -100123, first_name="Old")
        await subs.add_if_new(900, -100123, first_name="New")
        await session.commit()
        assert await subs.last_seen_user_id() == 900

    async def test_sync_delta_skips_old_users(self, session, monkeypatch):
        """Режим дельты заносит только id больше курсора (свежие регистрации)."""
        from app.services import mtproto_sync as mts
        from app.db.repositories import SubscriberRepository
        from app.config import get_settings

        st = get_settings()
        monkeypatch.setattr(st, "tracked_chat_ids", [-1001234567890], raising=False)
        monkeypatch.setattr(mts, "collect_participant_ids",
                            lambda: _coro([100, 500, 900, 1200]))
        subs = SubscriberRepository(session)
        await subs.add_if_new(900, -1001234567890, first_name="Known")
        await session.commit()

        # патчим фабрику сессий, чтобы sync работал поверх тестовой БД
        import sqlalchemy.ext.asyncio as aea
        class _Factory:
            def __call__(self):
                class _Ctx:
                    async def __aenter__(self): return session
                    async def __aexit__(self, *a): return False
                return _Ctx()
        monkeypatch.setattr(aea, "async_sessionmaker", lambda *a, **k: _Factory())
        monkeypatch.setattr(aea, "create_async_engine", lambda *a, **k: _Engine())
        class _Engine:
            async def dispose(self): pass
        monkeypatch.setattr("app.services.mtproto_sync.get_settings", lambda: st)

        res = await mts.sync_subscribers(first_run=False)
        # курсор = max(id в базе) = 900 ⇒ добавляется строго больше: только 1200
        assert res["added"] == 1
        for uid in (100, 500):
            assert await subs.get(uid) is None
        assert await subs.get(1200) is not None

    async def test_sync_first_run_adds_everyone(self, session, monkeypatch):
        from app.services import mtproto_sync as mts
        from app.db.repositories import SubscriberRepository
        from app.config import get_settings
        import sqlalchemy.ext.asyncio as aea

        st = get_settings()
        monkeypatch.setattr(st, "tracked_chat_ids", [-1001234567890], raising=False)
        monkeypatch.setattr(mts, "collect_participant_ids",
                            lambda: _coro([100, 200]))
        class _Factory:
            def __call__(self):
                class _Ctx:
                    async def __aenter__(self): return session
                    async def __aexit__(self, *a): return False
                return _Ctx()
        class _Engine:
            async def dispose(self): pass
        monkeypatch.setattr(aea, "async_sessionmaker", lambda *a, **k: _Factory())
        monkeypatch.setattr(aea, "create_async_engine", lambda *a, **k: _Engine())
        res = await mts.sync_subscribers(first_run=True)
        assert res["added"] == 2
        subs = SubscriberRepository(session)
        assert await subs.get(100) is not None and await subs.get(200) is not None

    def test_no_hardcoded_secrets(self):
        """Секретов в коде нет — только имена полей config и .env.example."""
        import inspect
        from app.services import mtproto_sync as mts
        src = inspect.getsource(mts)
        for frag in ("22406532", "8953437425", "github_pat", "AAE0h8"):
            assert frag not in src
