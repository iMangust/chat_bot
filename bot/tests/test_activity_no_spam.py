"""Фоновая активность без спама мгновенными DM (v1.5.9)."""
from __future__ import annotations

from _helpers import get_settings, select, func

# ---------------------------------------------------------------------------
# 6. Фоновая активность без спама (v1.5.9)
# ---------------------------------------------------------------------------
class TestNoActivitySpam:
    async def test_sticker_message_no_instant_dm(self, session):
        """Зачёт стикера не должен слать НИ ОДНОГО мгновенного DM."""
        from app.db.models import NotificationQueue, User
        from app.services.activity import ActivityService

        u = User(tg_id=777, first_name="Sticker", onboarded=True)
        session.add(u)
        await session.flush()

        class ExplodingBot:
            async def send_message(self, *a, **k):
                raise AssertionError("мгновенный DM за сообщение недопустим (v1.5.9)")

        svc = ActivityService(session, bot=ExplodingBot())
        entry = await svc.process_group_message(
            user_id=777, chat_id=-1001, message_id=1, text=None,
            has_media=True, media_type="sticker", is_reply=False,
            mentions_count=0, is_command=False)
        assert entry is not None and entry.is_counted
        # ни одного уведомления в очереди тоже нет (XP/стикер — не «важное» событие)
        n = (await session.execute(
            select(func.count()).select_from(NotificationQueue))).scalar_one()
        assert n == 0

    async def test_levelup_goes_to_queue_not_instant(self, session, monkeypatch):
        """Левелап — важное событие: ОДНО сообщение в очередь, без мгновенного DM."""
        from app.db.models import NotificationQueue, User
        from app.services import activity as act_mod

        # понизим порог XP так, чтобы одно сообщение дало локап
        s = get_settings()
        monkeypatch.setattr(s, "xp_per_message", 10_000, raising=False)

        u = User(tg_id=888, first_name="Leveler", onboarded=True, level=1, xp=0)
        session.add(u)
        await session.flush()

        class ExplodingBot:
            async def send_message(self, *a, **k):
                raise AssertionError("мгновенный DM за левелап недопустим (v1.5.9)")

        svc = act_mod.ActivityService(session, bot=ExplodingBot())
        await svc.process_group_message(
            user_id=888, chat_id=-1001, message_id=2, text="hello world",
            has_media=False, media_type=None, is_reply=False,
            mentions_count=0, is_command=False)

        rows = list((await session.execute(select(NotificationQueue))).scalars())
        assert len(rows) == 1
        assert rows[0].kind == "levelup"
        assert rows[0].user_id == 888
        assert not rows[0].sent

    async def test_reaction_levelup_queued(self, session, monkeypatch):
        """Левелап от реакции — тоже через очередь, без прямого send_message."""
        from app.db.models import NotificationQueue, User
        from app.services.activity import ActivityService

        s = get_settings()
        monkeypatch.setattr(s, "xp_per_message", 1, raising=False)

        a = User(tg_id=901, first_name="Giver", onboarded=True, level=1, xp=99999)
        b = User(tg_id=902, first_name="Taker", onboarded=True, level=1, xp=0)
        session.add_all([a, b])
        await session.flush()

        class ExplodingBot:
            async def send_message(self, *a, **k):
                raise AssertionError("мгновенный DM за реакцию недопустим (v1.5.9)")

        svc = ActivityService(session, bot=ExplodingBot())
        ok = await svc.process_reaction(from_user=901, to_user=902,
                                        chat_id=-1001, message_id=5, emoji="❤️")
        assert ok is True
        kinds = [(r.user_id, r.kind) for r in
                 (await session.execute(select(NotificationQueue))).scalars()]
        assert (901, "levelup") in kinds

    def test_notify_instant_removed(self):
        """Старый спам-механизм удалён из кода полностью."""
        import inspect
        from app.services import activity as act_mod
        src = inspect.getsource(act_mod)
        assert "_notify_instant" not in src
        assert "Зачтено" not in src  # текст «Зачтено … — бонусные XP» больше не шлётся
