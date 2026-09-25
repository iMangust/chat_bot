"""SQLAlchemy 2.0 модели (асинхронные).

Покрывает MVP: пользователи, лог сообщений, реакции, достижения, питомцы,
инвентарь/предметы, настройки чатов, очередь уведомлений.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, Float, ForeignKey, Index,
    Integer, JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    # MySQL: таблицы создаются сразу в utf8mb4 — 4-байтные эмодзи (🐾💬)
    # проходят даже если база по умолчанию utf8mb3. Для sqlite игнорируется.
    __table_args__ = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}


def _ta(*args):
    """Собирает __table_args__: индексы/констрейны + mysql utf8mb4."""
    return (*args, {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"})



# ---------------------------------------------------------------------------
# Пользователи
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(128), default="")
    last_name: Mapped[str | None] = mapped_column(String(128))
    lang: Mapped[str] = mapped_column(String(8), default="ru")

    level: Mapped[int] = mapped_column(Integer, default=1)
    xp: Mapped[int] = mapped_column(Integer, default=0)
    coins: Mapped[int] = mapped_column(Integer, default=0)

    streak_days: Mapped[int] = mapped_column(Integer, default=0)
    best_streak: Mapped[int] = mapped_column(Integer, default=0)
    last_active_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    onboarded: Mapped[bool] = mapped_column(Boolean, default=False)
    pet_name: Mapped[str | None] = mapped_column(String(64))
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    # кто пригласил пользователя (deep-link invite_<tg_id>) — для реф-системы
    referrer_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    # произвольные сервисные флаги (например _ref_credited) — без новых миграций
    settings_extra: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    messages_count: Mapped[int] = mapped_column(Integer, default=0)   # денормализованный счётчик
    reactions_given: Mapped[int] = mapped_column(Integer, default=0)
    reactions_received: Mapped[int] = mapped_column(Integer, default=0)

    pet: Mapped["Pet | None"] = relationship(back_populates="owner", uselist=False)


# ---------------------------------------------------------------------------
# Журналы активности
# ---------------------------------------------------------------------------
class ChatMessageLog(Base):
    __tablename__ = "chat_messages_log"
    __table_args__ = _ta(
        Index("ix_cml_user_created", "user_id", "created_at"),
        Index("ix_cml_chat_created", "chat_id", "created_at"),
        UniqueConstraint("chat_id", "message_id", name="uq_cml_chat_msg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    length: Mapped[int] = mapped_column(Integer, default=0)
    has_media: Mapped[bool] = mapped_column(Boolean, default=False)
    media_type: Mapped[str | None] = mapped_column(String(32))  # photo/sticker/voice/...
    is_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    mentions_count: Mapped[int] = mapped_column(Integer, default=0)
    is_counted: Mapped[bool] = mapped_column(Boolean, default=True)  # прошёл ли антифрод
    skip_reason: Mapped[str | None] = mapped_column(String(32))      # short/cooldown/duplicate/command
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReactionLog(Base):
    __tablename__ = "reactions_log"
    __table_args__ = _ta(
        Index("ix_rl_to_created", "to_user", "created_at"),
        UniqueConstraint("from_user", "message_id", "emoji", name="uq_reaction_once"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_user: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"))
    to_user: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    emoji: Mapped[str] = mapped_column(String(16))
    is_counted: Mapped[bool] = mapped_column(Boolean, default=True)  # антифрод-лимит суточный
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Достижения
# ---------------------------------------------------------------------------
class AchievementCategory(str, enum.Enum):
    activity = "activity"
    streak = "streak"
    reactions = "reactions"
    pet = "pet"
    social = "social"
    secret = "secret"


class AchievementRarity(str, enum.Enum):
    common = "common"
    rare = "rare"
    epic = "epic"
    legendary = "legendary"


class ConditionType(str, enum.Enum):
    """Тип счётчика, к которому привязано условие достижения."""
    messages_total = "messages_total"
    messages_day = "messages_day"
    streak_days = "streak_days"
    reactions_given = "reactions_given"
    reactions_received = "reactions_received"
    pet_feeds = "pet_feeds"
    pet_level = "pet_level"
    pet_walks = "pet_walks"
    invites = "invites"
    top1_day = "top1_day"
    level = "level"          # глобальный уровень пользователя
    coins_earned = "coins_earned"
    games_won = "games_won"  # победы в мини-играх (Этап 3.5)


class Achievement(Base):
    __tablename__ = "achievements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(String(16), default="🏅")
    category: Mapped[AchievementCategory] = mapped_column(Enum(AchievementCategory, native_enum=False))
    condition_type: Mapped[ConditionType] = mapped_column(Enum(ConditionType, native_enum=False))
    condition_value: Mapped[int] = mapped_column(Integer, default=1)
    reward_xp: Mapped[int] = mapped_column(Integer, default=0)
    reward_coins: Mapped[int] = mapped_column(Integer, default=0)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    rarity: Mapped[AchievementRarity] = mapped_column(
        Enum(AchievementRarity, native_enum=False), default=AchievementRarity.common
    )

    unlocks: Mapped[list["UserAchievement"]] = relationship(back_populates="achievement")


class UserAchievement(Base):
    __tablename__ = "user_achievements"
    __table_args__ = _ta(UniqueConstraint("user_id", "achievement_id", name="uq_user_ach"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"))
    achievement_id: Mapped[int] = mapped_column(Integer, ForeignKey("achievements.id", ondelete="CASCADE"))
    progress: Mapped[int] = mapped_column(Integer, default=0)
    unlocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    achievement: Mapped["Achievement"] = relationship(back_populates="unlocks")


# ---------------------------------------------------------------------------
# Тамагочи
# ---------------------------------------------------------------------------
class PetStage(str, enum.Enum):
    egg = "egg"            # 1-2
    baby = "baby"          # 3-5
    teen = "teen"          # 6-9
    adult = "adult"        # 10-14
    legendary = "legendary"  # 15+


class PetSpecies(str, enum.Enum):
    cat = "cat"           # + счастье от игр
    dog = "dog"           # + голод медленнее
    fox = "fox"           # + монеты с прогулок
    owl = "owl"           # + XP с тренировок
    dragon = "dragon"     # универсал, редкая стартовая (за 500 монет)


class Pet(Base):
    __tablename__ = "pets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # unique=True снимается в v1.4.7: у пользователя может быть архив прошлых
    # питомцев (generation/is_archived); «текущий» выбирается фильтром
    # is_archived=False (PetRepository.get_by_user).
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    species: Mapped[PetSpecies] = mapped_column(Enum(PetSpecies, native_enum=False), default=PetSpecies.cat)

    level: Mapped[int] = mapped_column(Integer, default=1)
    xp: Mapped[int] = mapped_column(Integer, default=0)
    stage: Mapped[PetStage] = mapped_column(Enum(PetStage, native_enum=False), default=PetStage.egg)

    hunger: Mapped[float] = mapped_column(Float, default=80.0)     # сытость 0..100
    happiness: Mapped[float] = mapped_column(Float, default=80.0)
    energy: Mapped[float] = mapped_column(Float, default=80.0)
    hygiene: Mapped[float] = mapped_column(Float, default=80.0)
    health: Mapped[float] = mapped_column(Float, default=100.0)

    strength: Mapped[int] = mapped_column(Integer, default=1)
    agility: Mapped[int] = mapped_column(Integer, default=1)
    intellect: Mapped[int] = mapped_column(Integer, default=1)

    is_sleeping: Mapped[bool] = mapped_column(Boolean, default=False)
    sleep_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    walk_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sick_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    last_update: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    born_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    settings_extra: Mapped[dict] = mapped_column(JSON, default=dict)  # окрас, аксессуары

    # --- история питомца (v1.4.7): «усыновление» нового вместо удаления старого ---
    # generation=1 — текущий питомец; предыдущие получают is_archived=True и
    # попадают в pet_history_screen («предыдущие питомцы»). Так статистика и
    # ачивки старого питомца не теряются при смене вида/имени.
    generation: Mapped[int] = mapped_column(Integer, default=1)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archive_reason: Mapped[str | None] = mapped_column(String(32))  # rehomed/grew_up

    owner: Mapped["User"] = relationship(back_populates="pet")
    inventory: Mapped[list["PetInventory"]] = relationship(back_populates="pet", cascade="all, delete-orphan")


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    icon: Mapped[str] = mapped_column(String(16), default="📦")
    type: Mapped[str] = mapped_column(String(32))  # food/toy/medicine/accessory/species/merch
    price: Mapped[int] = mapped_column(Integer, default=10)
    effect: Mapped[dict] = mapped_column(JSON, default=dict)  # {"hunger": +20, "happiness": +5}
    description: Mapped[str] = mapped_column(Text, default="")


class PetInventory(Base):
    __tablename__ = "pet_inventory"
    __table_args__ = _ta(UniqueConstraint("pet_id", "item_id", name="uq_pet_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pet_id: Mapped[int] = mapped_column(Integer, ForeignKey("pets.id", ondelete="CASCADE"))
    item_id: Mapped[int] = mapped_column(Integer, ForeignKey("items.id", ondelete="CASCADE"))
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    pet: Mapped["Pet"] = relationship(back_populates="inventory")
    item: Mapped["Item"] = relationship()


class PetActionLog(Base):
    __tablename__ = "pet_actions_log"
    __table_args__ = _ta(Index("ix_pal_pet_created", "pet_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pet_id: Mapped[int] = mapped_column(Integer, ForeignKey("pets.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(32))  # feed/play/wash/sleep/heal/walk/train/buy
    value: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PetFriend(Base):
    __tablename__ = "pet_friends"
    __table_args__ = _ta(UniqueConstraint("pet_id", "friend_pet_id", name="uq_pet_friend"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pet_id: Mapped[int] = mapped_column(Integer, ForeignKey("pets.id", ondelete="CASCADE"))
    friend_pet_id: Mapped[int] = mapped_column(Integer, ForeignKey("pets.id", ondelete="CASCADE"))
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Прочее
# ---------------------------------------------------------------------------
class ChannelSubscriber(Base):
    """Подписчики канала (для приветствия новичков в ЛС).

    Источник — апдейт chat_member (бот-админ канала + подписка на chat_member
    в allowed_updates) и фоновый скан get_chat_member_count. Поле welcomed_at
    гарантирует «не более одного приветствия» даже при повторных доставках
    апдейтов и перезапусках бота.
    """
    __tablename__ = "channel_subscribers"
    __table_args__ = _ta(
        Index("ix_channel_subscribers_first_seen", "first_seen"),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(128), default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    welcomed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatSettings(Base):
    __tablename__ = "chat_settings"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    welcome_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    welcome_text: Mapped[str | None] = mapped_column(Text)
    cooldown_sec: Mapped[int] = mapped_column(Integer, default=10)
    min_length: Mapped[int] = mapped_column(Integer, default=5)
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class NotificationQueue(Base):
    __tablename__ = "notifications_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text)
    send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    kind: Mapped[str] = mapped_column(String(32), default="info")  # achievement/pet/streak/daily


class LeaderboardSnapshot(Base):
    __tablename__ = "leaderboards_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period: Mapped[str] = mapped_column(String(16))     # day/week/all
    category: Mapped[str] = mapped_column(String(32))   # messages/reactions/level/pet
    data: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PetDuel(Base):
    """Недельные соревнования питомцев (PVP, Этап 6+).

    Питомцы дерутся «на характеристиках» (сила/ловкость/интеллект + уровень),
    без RNG-рулетки: честный расчёт в services.pet_duels. Счёт побед копится
    в неделе (week_key = ISO-неделя 'YYYY-Www'); по понедельникам планировщик
     берёт топ-3 по очам и выдаёт призы, после чего счёт обнуляется новым
    week_key (старые строки остаются как история — leaderboards_snapshot).
    """
    __tablename__ = "pet_duels"
    __table_args__ = _ta(
        UniqueConstraint("pet_id", "week_key", name="uq_pet_duel_week"),
        Index("ix_pd_week_score", "week_key", "score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pet_id: Mapped[int] = mapped_column(Integer, ForeignKey("pets.id", ondelete="CASCADE"))
    week_key: Mapped[str] = mapped_column(String(12))   # '2026-W39'
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[int] = mapped_column(Integer, default=0)  # рейтинг внутри недели
    fights: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UserStat(Base):
    """Пользовательские счётчики для ачивок (invites и т.п.)."""
    __tablename__ = "user_stats"
    __table_args__ = _ta(UniqueConstraint("user_id", "key", name="uq_user_stat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(32))
    value: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class NotificationSetting(Base):
    """Персональные настройки уведомлений (Этап 6, экран ⚙️ Настройки)."""
    __tablename__ = "notification_settings"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"),
                                         primary_key=True, autoincrement=False)
    pet_reminders: Mapped[bool] = mapped_column(Boolean, default=True)   # «питомец скучает»
    streak_reminders: Mapped[bool] = mapped_column(Boolean, default=True)  # стрик под угрозой / сгорел
    achievement_notifications: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_report: Mapped[bool] = mapped_column(Boolean, default=True)    # ежедневный отчёт 20:00 UTC
