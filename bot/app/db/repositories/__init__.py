"""Репозитории: слой запросов к БД (чистые async-функции, без бизнес-логики)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    ChatMessageLog, NotificationSetting, Pet, PetActionLog, ReactionLog,
    User, UserAchievement, UserStat,
)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, tg_id: int) -> User | None:
        return await self.session.get(User, tg_id)

    async def get_with_pet(self, tg_id: int) -> User | None:
        stmt = (
            select(User)
            .options(selectinload(User.pet))
            .where(User.tg_id == tg_id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_or_create(self, tg_id: int, first_name: str = "",
                            username: str | None = None) -> User:
        user = await self.get(tg_id)
        if user is None:
            user = User(tg_id=tg_id, first_name=first_name, username=username)
            self.session.add(user)
            await self.session.flush()
        else:
            # обновляем профильные поля, если изменились
            if first_name and user.first_name != first_name:
                user.first_name = first_name
            if username is not None and user.username != username:
                user.username = username
        return user

    async def add_xp_coins(self, tg_id: int, xp: int = 0, coins: int = 0) -> None:
        await self.session.execute(
            update(User).where(User.tg_id == tg_id)
            .values(xp=User.xp + xp, coins=User.coins + coins)
        )

    async def top_by(self, column: str, limit: int = 10) -> list[User]:
        col = getattr(User, column)
        stmt = select(User).order_by(col.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def active_since(self, since: datetime) -> list[int]:
        """ID пользователей, у которых была засчитанная активность после `since`."""
        stmt = (
            select(ChatMessageLog.user_id)
            .where(ChatMessageLog.created_at >= since, ChatMessageLog.is_counted.is_(True))
            .distinct()
        )
        return list((await self.session.execute(stmt)).scalars())

    # ---- пользовательские счётчики (invites и т.п.) ----
    async def bump_stat(self, tg_id: int, key: str, delta: int = 1) -> int:
        """Атомарно увеличивает счётчик UserStat; возвращает новое значение."""
        row = (await self.session.execute(
            select(UserStat).where(UserStat.user_id == tg_id, UserStat.key == key)
        )).scalar_one_or_none()
        if row is None:
            row = UserStat(user_id=tg_id, key=key, value=max(0, delta))
            self.session.add(row)
        else:
            row.value = max(0, row.value + delta)
        await self.session.flush()
        return row.value

    async def get_stat(self, tg_id: int, key: str) -> int:
        row = (await self.session.execute(
            select(UserStat).where(UserStat.user_id == tg_id, UserStat.key == key)
        )).scalar_one_or_none()
        return row.value if row else 0

    # ---- персональные настройки уведомлений ----
    async def notif_settings(self, tg_id: int) -> NotificationSetting:
        ns = await self.session.get(NotificationSetting, tg_id)
        if ns is None:
            ns = NotificationSetting(user_id=tg_id)
            self.session.add(ns)
            await self.session.flush()
        return ns

    async def top_period_messages(self, since: datetime, limit: int = 10) -> list[tuple[User, int]]:
        """Топ за период по засчитанным сообщениям: [(User, cnt)]."""
        cnt = func.count().label("cnt")
        sub = (
            select(ChatMessageLog.user_id.label("uid"), cnt)
            .where(ChatMessageLog.created_at >= since, ChatMessageLog.is_counted.is_(True))
            .group_by(ChatMessageLog.user_id)
            .order_by(cnt.desc())
            .limit(limit)
            .subquery()
        )
        stmt = (
            select(User, sub.c.cnt)
            .join(sub, sub.c.uid == User.tg_id)
            .order_by(sub.c.cnt.desc())
        )
        return [(r[0], r[1]) for r in (await self.session.execute(stmt)).all()]

    async def top_period_reactions(self, since: datetime, limit: int = 10) -> list[tuple[User, int]]:
        """Топ за период по ПОЛУЧЕННЫМ реакциям (засчитанным)."""
        cnt = func.count().label("cnt")
        sub = (
            select(ReactionLog.to_user.label("uid"), cnt)
            .where(ReactionLog.created_at >= since, ReactionLog.is_counted.is_(True))
            .group_by(ReactionLog.to_user)
            .order_by(cnt.desc())
            .limit(limit)
            .subquery()
        )
        stmt = (
            select(User, sub.c.cnt)
            .join(sub, sub.c.uid == User.tg_id)
            .order_by(sub.c.cnt.desc())
        )
        return [(r[0], r[1]) for r in (await self.session.execute(stmt)).all()]


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------
class ActivityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def log_message(self, entry: ChatMessageLog) -> ChatMessageLog:
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def messages_count(self, tg_id: int, since: datetime | None = None) -> int:
        stmt = (
            select(func.count())
            .select_from(ChatMessageLog)
            .where(
                ChatMessageLog.user_id == tg_id,
                ChatMessageLog.is_counted.is_(True),
            )
        )
        if since is not None:
            stmt = stmt.where(ChatMessageLog.created_at >= since)
        return (await self.session.execute(stmt)).scalar_one()

    async def bump_counters(self, tg_id: int) -> None:
        await self.session.execute(
            update(User).where(User.tg_id == tg_id)
            .values(messages_count=User.messages_count + 1)
        )

    async def log_reaction(self, entry: ReactionLog) -> bool:
        """Возвращает True, если реакция новая (не дубль)."""
        dup = await self.session.execute(
            select(func.count()).select_from(ReactionLog).where(
                ReactionLog.from_user == entry.from_user,
                ReactionLog.message_id == entry.message_id,
                ReactionLog.emoji == entry.emoji,
            )
        )
        if dup.scalar_one() > 0:
            return False
        self.session.add(entry)
        await self.session.flush()
        return True

    async def day_totals(self, day: datetime) -> list[tuple[int, int]]:
        """Топ за день: [(user_id, cnt)] — используется для лидерборда/ачивки top1_day."""
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        stmt = (
            select(ChatMessageLog.user_id, func.count().label("cnt"))
            .where(
                ChatMessageLog.created_at >= start,
                ChatMessageLog.created_at < start + timedelta(days=1),
                ChatMessageLog.is_counted.is_(True),
            )
            .group_by(ChatMessageLog.user_id)
            .order_by(func.count().desc())
            .limit(10)
        )
        return [(r[0], r[1]) for r in (await self.session.execute(stmt)).all()]


# ---------------------------------------------------------------------------
# Pets
# ---------------------------------------------------------------------------
class PetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_user(self, user_id: int) -> Pet | None:
        stmt = select(Pet).where(Pet.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create(self, pet: Pet) -> Pet:
        self.session.add(pet)
        await self.session.flush()
        return pet

    async def all_ids(self) -> list[int]:
        stmt = select(Pet.id)
        return list((await self.session.execute(stmt)).scalars())

    async def count_actions(self, pet_id: int, action: str) -> int:
        stmt = (
            select(func.count())
            .select_from(PetActionLog)
            .where(PetActionLog.pet_id == pet_id, PetActionLog.action == action)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def log_action(self, pet_id: int, action: str, value: int = 0,
                         meta: dict | None = None) -> None:
        self.session.add(PetActionLog(pet_id=pet_id, action=action,
                                      value=value, meta=meta or {}))
        await self.session.flush()

    # ---- друзья и соревнование питомцев (Этап 6) ----
    async def friends(self, pet_id: int) -> list["PetFriend"]:
        from app.db.models import PetFriend
        stmt = select(PetFriend).where(PetFriend.pet_id == pet_id)
        return list((await self.session.execute(stmt)).scalars())

    async def add_friend(self, pet_id: int, friend_pet_id: int) -> bool:
        """Добавляет дружбу в обе стороны; False — если уже дружат."""
        from app.db.models import PetFriend
        dup = (await self.session.execute(
            select(PetFriend).where(PetFriend.pet_id == pet_id,
                                    PetFriend.friend_pet_id == friend_pet_id)
        )).scalar_one_or_none()
        if dup is not None:
            return False
        self.session.add(PetFriend(pet_id=pet_id, friend_pet_id=friend_pet_id))
        self.session.add(PetFriend(pet_id=friend_pet_id, friend_pet_id=pet_id))
        await self.session.flush()
        return True

    async def top_pets(self, limit: int = 10) -> list[Pet]:
        """Соревнование питомцев: по уровню, затем по XP."""
        stmt = select(Pet).order_by(Pet.level.desc(), Pet.xp.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())


# ---------------------------------------------------------------------------
# Notification settings
# ---------------------------------------------------------------------------
class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(self, user_id: int) -> NotificationSetting:
        ns = await self.session.get(NotificationSetting, user_id)
        if ns is None:
            ns = NotificationSetting(user_id=user_id)
            self.session.add(ns)
            await self.session.flush()
        return ns


# ---------------------------------------------------------------------------
# Achievements (прогресс)
# ---------------------------------------------------------------------------
class AchievementRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_progress_rows(self, user_id: int) -> list[UserAchievement]:
        from app.db.models import Achievement
        stmt = (
            select(UserAchievement)
            .join(Achievement, Achievement.id == UserAchievement.achievement_id)
            .where(UserAchievement.user_id == user_id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def upsert_progress(self, user_id: int, achievement_id: int,
                              progress: int) -> bool:
        """Обновляет прогресс; возвращает True если ачивка ТОЛЬКО ЧТО разблокирована."""
        now = datetime.now(timezone.utc)
        row = (await self.session.execute(
            select(UserAchievement).where(
                UserAchievement.user_id == user_id,
                UserAchievement.achievement_id == achievement_id,
            )
        )).scalar_one_or_none()

        if row is None:
            from app.db.models import Achievement
            ach = await self.session.get(Achievement, achievement_id)
            just_unlocked = ach is not None and progress >= ach.condition_value
            row = UserAchievement(user_id=user_id, achievement_id=achievement_id,
                                  progress=progress,
                                  unlocked_at=now if just_unlocked else None)
            self.session.add(row)
            await self.session.flush()
            return just_unlocked

        was_unlocked = row.unlocked_at is not None
        row.progress = max(row.progress, progress)
        if not was_unlocked:
            from app.db.models import Achievement
            ach = await self.session.get(Achievement, achievement_id)
            if ach and row.progress >= ach.condition_value:
                row.unlocked_at = now
                return True
        return False
