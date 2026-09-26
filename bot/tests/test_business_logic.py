"""Тесты бизнес-логики: achievements, leaderboard, activity/tracker.

v1.5.22: эти модули не покрывались тестами вовсе — регрессии в выдаче
достижений, подсчёте топов и антифрод-кулдаунах остались бы незамеченными.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import (
    Achievement, ChatMessageLog, NotificationQueue, ReactionLog, User,
    UserAchievement,
)
from app.services import leaderboard as lb
from app.services.achievements import ACHIEVEMENTS, AchievementService, seed_achievements
from app.services.activity import ActivityService


async def mk_user(session, tg_id: int, **kw) -> User:
    u = User(tg_id=tg_id, first_name=f"U{tg_id}", **kw)
    session.add(u)
    await session.flush()
    return u


# ==================================================================== achievements

class TestSeed:
    async def test_seed_creates_all_and_is_idempotent(self, session):
        assert await seed_achievements(session) == len(ACHIEVEMENTS)
        # повторный сид не создаёт дублей
        assert await seed_achievements(session) == 0
        rows = (await session.execute(select(Achievement))).scalars().all()
        assert len(rows) == len(ACHIEVEMENTS)
        assert len({a.code for a in rows}) == len(ACHIEVEMENTS)

    async def test_reference_data_consistency(self):
        codes = [a.code for a in ACHIEVEMENTS]
        assert len(codes) == len(set(codes)), "дубли кодов достижений"
        for a in ACHIEVEMENTS:
            assert a.condition_value > 0
            assert a.reward_xp >= 0 and a.reward_coins >= 0
            # награды монотонны: более высокий порог не может стоить дешевле XP
        by_thr = sorted((a for a in ACHIEVEMENTS
                        if a.condition_type.value == "messages_total" and not a.is_hidden),
                       key=lambda a: a.condition_value)
        xps = [a.reward_xp for a in by_thr]
        assert xps == sorted(xps), f"немонотонные XP-награды: {xps}"


class TestCheck:
    async def _svc(self, session):
        await seed_achievements(session)
        return AchievementService(session)

    async def test_unlocks_only_reached_thresholds_once(self, session):
        svc = await self._svc(session)
        u = await mk_user(session, 9001)
        first = await svc.check(u.tg_id, {"messages_total": 10})
        assert [a.code for a in first] == ["msg_10"]
        # повторный check с тем же прогрессом — ничего нового (защита от фарма наград)
        assert await svc.check(u.tg_id, {"messages_total": 10}) == []
        # рост счётчика открывает следующий порог
        second = await svc.check(u.tg_id, {"messages_total": 100})
        assert [a.code for a in second] == ["msg_100"]

    async def test_rewards_granted_on_unlock(self, session):
        svc = await self._svc(session)
        u = await mk_user(session, 9002, xp=0, level=1, coins=0)
        d = next(a for a in ACHIEVEMENTS if a.code == "msg_10")
        await svc.check(u.tg_id, {"messages_total": 10})
        await session.refresh(u)
        assert u.coins == d.reward_coins
        assert u.xp + (u.level - 1) * 100 >= d.reward_xp  # xp мог конвертироваться в уровни

    async def test_hidden_requires_force_codes(self, session):
        svc = await self._svc(session)
        u = await mk_user(session, 9003)
        # без force_codes скрытые не открываются даже при большом счётчике
        got = await svc.check(u.tg_id, {"messages_total": 10000})
        assert "night_owl" not in [a.code for a in got]
        # с force_codes — открываются
        got2 = await svc.check(u.tg_id, {"messages_total": 1}, force_codes=["night_owl"])
        assert [a.code for a in got2] == ["night_owl"]

    async def test_zero_counters_unlock_nothing(self, session):
        svc = await self._svc(session)
        u = await mk_user(session, 9004)
        assert await svc.check(u.tg_id, {"messages_total": 0, "streak_days": 0}) == []

    async def test_progress_persists_and_partial_shown(self, session):
        svc = await self._svc(session)
        u = await mk_user(session, 9005)
        await svc.check(u.tg_id, {"messages_total": 5})  # ниже msg_10 — только прогресс
        row = (await session.execute(
            select(UserAchievement).where(UserAchievement.user_id == u.tg_id)
        )).scalars().first()
        assert row is not None and row.progress == 5 and row.unlocked_at is None

    async def test_unlock_by_code_unknown_and_twice(self, session):
        svc = await self._svc(session)
        u = await mk_user(session, 9006)
        assert await svc.unlock_by_code(u.tg_id, "no_such_code") is None
        assert await svc.unlock_by_code(u.tg_id, "first_steps") is not None
        # второй вызов — уже открыто, None и без двойной награды
        before = u.coins
        assert await svc.unlock_by_code(u.tg_id, "first_steps") is None
        await session.refresh(u)
        assert u.coins == before

    async def test_list_for_user_shows_progress(self, session):
        svc = await self._svc(session)
        u = await mk_user(session, 9007)
        await svc.check(u.tg_id, {"messages_total": 10})
        listing = await svc.list_for_user(u.tg_id)
        codes = [a.code for a, _ in listing]
        assert "msg_10" in codes
        # открытые идут первыми (сортировка экрана 🏆)
        assert listing[0][0].code == "msg_10" and listing[0][1].unlocked_at is not None


# ======================================================================= leaderboard

class TestLeaderboard:
    async def test_top_messages_all_time_and_window(self, session):
        now = datetime.now(timezone.utc)
        u1 = await mk_user(session, 9101, messages_count=5)
        u2 = await mk_user(session, 9102, messages_count=9)
        await mk_user(session, 9103, messages_count=0)
        alltime = await lb.top_messages(session, since=None)
        assert [(u.tg_id, c) for u, c in alltime] == [(9102, 9), (9101, 5)]

        # окно: учитываются только is_counted=True внутри since
        session.add_all([
            ChatMessageLog(user_id=u1.tg_id, chat_id=-1, message_id=1, created_at=now),
            ChatMessageLog(user_id=u1.tg_id, chat_id=-1, message_id=2,
                           created_at=now, is_counted=False),  # антифрод — мимо
            ChatMessageLog(user_id=u2.tg_id, chat_id=-1, message_id=3,
                           created_at=now - timedelta(days=30)),  # вне окна — мимо
            ChatMessageLog(user_id=u2.tg_id, chat_id=-1, message_id=4, created_at=now),
        ])
        await session.flush()
        day_top = await lb.top_messages(session, since=now - timedelta(hours=1))
        assert dict((u.tg_id, c) for u, c in day_top) == {9101: 1, 9102: 1}

    async def test_top_reactions_direction(self, session):
        giver = await mk_user(session, 9111)
        receiver = await mk_user(session, 9112)
        session.add(ReactionLog(from_user=giver.tg_id, to_user=receiver.tg_id,
                                chat_id=-1, message_id=1, emoji="🔥"))
        await session.flush()
        received = await lb.top_reactions(session, since=None)
        given = await lb.top_reactions_given(session, since=None)
        # денормализованные счётчики обнулены — локальный топ считается по логу
        recv_local = await lb.top_reactions(session, since=datetime.now(timezone.utc) - timedelta(hours=1))
        given_local = await lb.top_reactions_given(session, since=datetime.now(timezone.utc) - timedelta(hours=1))
        assert [u.tg_id for u, _ in recv_local] == [receiver.tg_id]
        assert [u.tg_id for u, _ in given_local] == [giver.tg_id]
        assert received == [] and given == []  # users.reactions_* никто не инкрементил

    async def test_overall_top_combines_sections(self, session):
        await mk_user(session, 9121, messages_count=100, level=2)
        await mk_user(session, 9122, reactions_received=50, level=1)
        overall = await lb.overall_top(session, since=None, limit=10)
        ids = [u.tg_id for u, _, _ in overall]
        assert set(ids) == {9121, 9122}
        # места: у каждого своя сильная секция; avg place больше у «односторонних»
        places = {u.tg_id: p for u, _, p in overall}
        assert places[9121]["talk"] == 1
        assert places[9122]["react"] == 1

    async def test_empty_leaderboards(self, session):
        assert await lb.top_messages(session, since=None) == []
        assert await lb.overall_top(session, since=None) == []


# ========================================================================= tracker

def _mk_activity_service(session):
    # Redis недоступен в тестах -> mem-fallback кулдаунов (см. conftest REDIS_URL)
    return ActivityService(session, bot=None)


async def _post_msg(svc, user_id, message_id, text="нормальный текст сообщения",
                    is_command=False, **kw):
    return await svc.process_group_message(
        user_id=user_id, chat_id=-100, message_id=message_id, text=text,
        has_media=False, media_type=None, is_reply=False, mentions_count=0,
        is_command=is_command, **kw)


class TestActivityTracker:
    async def test_unregistered_user_returns_none(self, session):
        svc = _mk_activity_service(session)
        assert await _post_msg(svc, 55555, 1) is None

    async def test_banned_user_returns_none(self, session):
        await mk_user(session, 9201, onboarded=True, is_banned=True)
        svc = _mk_activity_service(session)
        assert await _post_msg(svc, 9201, 1) is None

    async def test_not_onboarded_private_chat_skipped(self, session):
        await mk_user(session, 9202, onboarded=False)
        svc = _mk_activity_service(session)
        # чат != пользователь и не онборден — игнор, БД не трогаем
        assert await _post_msg(svc, 9202, 1) is None
        assert (await session.execute(
            select(ChatMessageLog).where(ChatMessageLog.user_id == 9202)
        )).scalars().all() == []

    async def test_counts_xp_coins_streak_and_denormalized(self, session):
        u = await mk_user(session, 9203, onboarded=True, xp=0, level=1, coins=0)
        svc = _mk_activity_service(session)
        entry = await _post_msg(svc, 9203, 101)
        assert entry is not None and entry.is_counted
        await session.refresh(u)
        assert u.messages_count == 1
        assert u.coins == 1                      # coins_per_message_cap по дефолту
        assert u.streak_days == 1 and u.best_streak == 1
        # вторая мгновенная реплика — кулдаун, засчитана не будет
        e2 = await _post_msg(svc, 9203, 102, text="ещё один длинный текст")
        assert e2 is not None and not e2.is_counted and e2.skip_reason == "cooldown"
        await session.refresh(u)
        assert u.messages_count == 1             # счётчик не вырос

    async def test_short_and_command_are_logged_but_not_counted(self, session):
        u = await mk_user(session, 9204, onboarded=True)
        svc = _mk_activity_service(session)
        e1 = await _post_msg(svc, 9204, 201, text="ok")   # короче MIN_MESSAGE_LENGTH=5
        assert e1.skip_reason == "short" and not e1.is_counted
        e2 = await _post_msg(svc, 9204, 202, text="/help команда", is_command=True)
        assert e2.skip_reason == "command" and not e2.is_counted
        await session.refresh(u)
        assert u.messages_count == 0

    async def test_first_counted_message_unlocks_achievement(self, session):
        # 10 сообщений подряд открывают msg_10 (кулдаун отключаем monkeypatchem;
        # activity импортирует set_cooldown в свой namespace — правим там)
        import app.services.activity as act_mod

        async def no_cd(key, ttl):
            return True
        monkey = pytest.MonkeyPatch()
        monkey.setattr(act_mod, "set_cooldown", no_cd)
        try:
            # seed нужен заранее: AchievementService создаётся внутри конструктора
            await seed_achievements(session)
            u = await mk_user(session, 9205, onboarded=True, coins=0, xp=0, level=1)
            svc = _mk_activity_service(session)
            for i in range(10):
                await _post_msg(svc, 9205, 300 + i)
            unlocked = (await session.execute(
                select(UserAchievement).where(UserAchievement.user_id == 9205)
            )).scalars().all()
            ach_ids = {r.achievement_id for r in unlocked if r.unlocked_at is not None}
            msg10 = (await session.execute(
                select(Achievement).where(Achievement.code == "msg_10")
            )).scalar_one()
            assert msg10.id in ach_ids
            await session.refresh(u)
            assert u.messages_count == 10
            # уведомление об ачивке попало в очередь
            notes = (await session.execute(
                select(NotificationQueue).where(NotificationQueue.user_id == 9205)
            )).scalars().all()
            assert any(n.kind == "achievement" for n in notes)
        finally:
            monkey.undo()

    async def test_streak_advances_next_day(self, session):
        u = await mk_user(session, 9206, onboarded=True)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        u.last_active_date = yesterday
        u.streak_days = 3
        await session.flush()
        svc = _mk_activity_service(session)
        await _post_msg(svc, 9206, 401)
        await session.refresh(u)
        assert u.streak_days == 4
        assert u.best_streak == 4

    async def test_streak_resets_after_gap(self, session):
        u = await mk_user(session, 9207, onboarded=True)
        u.last_active_date = datetime.now(timezone.utc) - timedelta(days=5)
        u.streak_days = 10
        u.best_streak = 10
        await session.flush()
        svc = _mk_activity_service(session)
        await _post_msg(svc, 9207, 402)
        await session.refresh(u)
        assert u.streak_days == 1
        assert u.best_streak == 10  # рекорд сохраняется
