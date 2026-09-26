"""Репозитории: слой запросов к БД (чистые async-функции, без бизнес-логики)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    ChatMessageLog, NotificationSetting, Pet, PetActionLog, ReactionLog,
    User, UserAchievement, UserStat, utcnow,
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

    # ---- реферальная система ----
    async def set_referrer(self, tg_id: int, referrer_id: int) -> bool:
        """Запоминаем пригласившего. True — если запись создана впервые."""
        user = await self.get(tg_id)
        if user is None or user.tg_id == referrer_id:
            return False
        if user.referrer_id == referrer_id:
            return False
        first_time = user.referrer_id is None
        user.referrer_id = referrer_id
        await self.session.flush()
        return first_time

    async def count_invited(self, referrer_id: int) -> int:
        """Сколько пользователей приведено по ссылке referrer_id."""
        return (await self.session.execute(
            select(func.count()).select_from(User).where(User.referrer_id == referrer_id)
        )).scalar_one()

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

    async def get_message_author(self, chat_id: int, message_id: int) -> int | None:
        """Автор сообщения из локального лога (None — если сообщение не видели)."""
        stmt = (
            select(ChatMessageLog.user_id)
            .where(ChatMessageLog.chat_id == chat_id,
                   ChatMessageLog.message_id == message_id)
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def log_message(self, entry: ChatMessageLog) -> ChatMessageLog:
        """Пишет запись лога, идемпотентно по (chat_id, message_id).

        Telegram может доставить один и тот же апдейт повторно (ретраи
        long-polling/webhook после таймаута) — без дедупликации это плодит
        дубли в статистике. Диалект upsert выбирается строго по имени диалекта
        соединения: sqlite/postgres имеют ON CONFLICT, MySQL — только
        INSERT IGNORE / ON DUPLICATE KEY. Жёсткий выбор sqlite-варианта для
        всего, что не postgres, ронял трекер на проде с MySQL
        (UnsupportedCompilationError: visit_on_conflict_do_nothing).
        """
        values = dict(
            user_id=entry.user_id, chat_id=entry.chat_id,
            message_id=entry.message_id, length=entry.length,
            has_media=entry.has_media, media_type=entry.media_type,
            is_reply=entry.is_reply, mentions_count=entry.mentions_count,
            is_counted=entry.is_counted, skip_reason=entry.skip_reason,
            created_at=entry.created_at,
        )
        dialect = self.session.bind.dialect.name if self.session.bind else "sqlite"
        try:
            if dialect in ("sqlite", "postgresql"):
                if dialect == "postgresql":
                    from sqlalchemy.dialects.postgresql import insert as ins
                else:
                    from sqlalchemy.dialects.sqlite import insert as ins
                stmt = ins(ChatMessageLog).values(**values).on_conflict_do_nothing(
                    index_elements=["chat_id", "message_id"]
                )
            elif dialect.startswith("mysql"):
                from sqlalchemy.dialects.mysql import insert as ins
                # UNIQUE (chat_id, message_id) + INSERT IGNORE — mysql-аналог do-nothing
                stmt = ins(ChatMessageLog).values(**values).prefix_with("IGNORE")
            else:  # неизвестный диалект — обычная вставка (дедуп не гарантируется)
                stmt = insert(ChatMessageLog).values(**values)
            await self.session.execute(stmt)
        except IntegrityError:
            # гонка повторных доставок / отсутствие UNIQUE-индекса на старой БД:
            # конфликт по (chat_id, message_id) означает «уже записано» — ок.
            await self.session.rollback()
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

    async def daily_counts(self, tg_id: int, days: int = 7,
                           since: datetime | None = None) -> "dict[str, int]":
        """Сообщения по дням (для графика в карточке профиля).

        Возвращает {'2026-09-19': 12, ...}; пустые дни отсутствуют — рисовальщик
        сам достраивает нули. Даты нормализуются к UTC-полудню, чтобы bucket
        был стабильным на SQLite (TEXT) и Postgres (timestamptz).
        """
        from datetime import timezone as _tz
        now = since or datetime.now(_tz.utc)
        start = now - timedelta(days=days - 1)
        day_expr = func.date(ChatMessageLog.created_at)
        stmt = (
            select(day_expr.label("d"), func.count().label("c"))
            .select_from(ChatMessageLog)
            .where(
                ChatMessageLog.user_id == tg_id,
                ChatMessageLog.is_counted.is_(True),
                ChatMessageLog.created_at >= start.replace(hour=0, minute=0, second=0, microsecond=0),
            )
            .group_by("d")
        )
        rows = (await self.session.execute(stmt)).all()
        out: dict[str, int] = {}
        for d, c in rows:
            key = d if isinstance(d, str) else d.isoformat()
            out[key] = int(c)
        return out

    async def media_breakdown(self, tg_id: int,
                              since: datetime | None = None) -> dict[str, int]:
        """Разбивка засчитанных сообщений по типам (v1.4.7).

        Ключи: text / photo / video / audio(голос+музыка) / voice / video_note
        (кружок) / sticker / animation / document / poll / other; отдельно —
        reply (ответы) и mentions (сумма упоминаний). Специальные флаги
        is_reply/mentions_count складываются поверх типа, поэтому один и тот
        же message может попасть и в «photo», и в «reply».
        """
        cond = [ChatMessageLog.user_id == tg_id, ChatMessageLog.is_counted.is_(True)]
        if since is not None:
            cond.append(ChatMessageLog.created_at >= since)
        stmt = (
            select(ChatMessageLog.media_type, func.count(),
                   func.coalesce(func.sum(ChatMessageLog.is_reply.cast(Integer)), 0),
                   func.coalesce(func.sum(ChatMessageLog.mentions_count), 0))
            .where(*cond)
            .group_by(ChatMessageLog.media_type)
        )
        rows = (await self.session.execute(stmt)).all()
        out: dict[str, int] = {}
        replies = 0
        mentions = 0
        known = {"text", "voice", "audio", "video_note", "video", "animation",
                 "sticker", "photo", "document", "poll"}
        for mtype, cnt, rep, men in rows:
            # None/"" — обычное текстовое сообщение; всё прочее неизвестное → other
            mt = (mtype or "").lower()
            key = mt if mt in known else ("text" if not mt else "other")
            out[key] = out.get(key, 0) + int(cnt)
            replies += int(rep or 0)
            mentions += int(men or 0)
        if replies:
            out["reply"] = replies
        if mentions:
            out["mentions"] = mentions
        return out

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
        """Текущий (неархивный) питомец пользователя.

        Без фильтра is_archived select вернул бы несколько строк (архив +
        текущий) и упал с MultipleResultsFound: при «смене» питомец не
        удаляется, а уходит в архив.
        """
        stmt = select(Pet).where(Pet.user_id == user_id,
                                 Pet.is_archived.is_(False))
        return (await self.session.execute(stmt)).scalars().first()

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

    # ---- друзья и соревнование питомцев ----
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


# ---------------------------------------------------------------------------
# Подписчики канала (приветствие новичков)
# ---------------------------------------------------------------------------
class SubscriberRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_if_new(self, user_id: int, chat_id: int,
                         first_name: str = "", username: str | None = None,
                         reset_welcome: bool = False) -> bool:
        """Заносит подписчика; True — если он ждёт приветствия (pending).

        Идемпотентно к повторным доставкам апдейтов: опираемся на PK user_id,
        конфликт молча пропускаем (already known). Если запись уже есть, но
        приветствие НЕ было доставлено (например, ЛС были закрыты), обновляем
        имя/username и возвращаем True — иначе «вечные pending» так и не
        дождались бы доставки: welcome_pending_subscribers берёт имя из базы.

        Приветствованный (welcomed_at) пользователь остаётся приветствованным:
        сброс отметки — только по явному reset_welcome=True (реальное событие
        вступления в ДРУГОЙ отслеживаемый чат). Иначе /start или первое
        сообщение после рестарта вернули бы новичка в очередь и он получил бы
        повторное DM-приветствие.
        """
        from app.db.models import ChannelSubscriber
        exists = await self.session.get(ChannelSubscriber, user_id)
        if exists is not None:
            if first_name:
                exists.first_name = first_name
            if username:
                exists.username = username
            if exists.welcomed_at is None:
                return True
            if (reset_welcome and chat_id and exists.chat_id != chat_id
                    and exists.welcome_sent_chat_id != chat_id):
                # Реальное вступление в ДРУГОЙ чат (не тот, где зарегистрирован,
                # и не тот, ради которого уже слали приветствие) — разрешаем
                # ещё одно DM.
                exists.chat_id = chat_id
                exists.welcomed_at = None
                return True
            return False
        try:
            self.session.add(ChannelSubscriber(
                user_id=user_id, chat_id=chat_id,
                first_name=first_name or "", username=username,
            ))
            await self.session.flush()
            return True
        except IntegrityError:  # гонка параллельных апдейтов
            await self.session.rollback()
            return False

    async def pending_welcomes(self, limit: int = 20):
        """Новые подписчики без отправленного приветствия."""
        from app.db.models import ChannelSubscriber
        stmt = (select(ChannelSubscriber)
                .where(ChannelSubscriber.welcomed_at.is_(None))
                .order_by(ChannelSubscriber.first_seen)
                .limit(limit))
        return list((await self.session.execute(stmt)).scalars())

    async def get(self, user_id: int):
        from app.db.models import ChannelSubscriber
        return await self.session.get(ChannelSubscriber, user_id)

    async def reset_welcome(self, user_id: int) -> None:
        """Снимает отметку приветствия (повторный вход в другой чат)."""
        from app.db.models import ChannelSubscriber
        row = await self.session.get(ChannelSubscriber, user_id)
        if row is not None:
            row.welcomed_at = None
            await self.session.commit()

    async def last_seen_user_id(self) -> int | None:
        """Максимальный известный user_id (курсор «новых» подписчиков).

        Telegram-ID монотонно возрастают: всё, что больше курсора, — свежая
        регистрация в Telegram и кандидат на welcome-рассылку. Используется
        service-ботом MTProto (get_full_channel.participants), который видит
        список участников канала целиком (Bot API — нет).
        """
        from app.db.models import ChannelSubscriber
        stmt = select(func.max(ChannelSubscriber.user_id))
        v = (await self.session.execute(stmt)).scalar_one_or_none()
        return int(v) if v is not None else None

    async def mark_welcomed(self, user_id: int, chat_id: int | None = None) -> None:
        """Отмечает приветствие доставленным; запоминает чат-основание (v1.5.10)."""
        from app.db.models import ChannelSubscriber
        row = await self.session.get(ChannelSubscriber, user_id)
        if row is not None and row.welcomed_at is None:
            row.welcomed_at = utcnow()
            if chat_id is not None:
                row.welcome_sent_chat_id = chat_id

    async def count(self) -> int:
        from app.db.models import ChannelSubscriber
        stmt = select(func.count()).select_from(ChannelSubscriber)
        return int((await self.session.execute(stmt)).scalar_one())
