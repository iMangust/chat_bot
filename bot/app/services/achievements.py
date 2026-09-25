"""Сервис достижений: сид справочника, проверка условий, награды.

Принципы защиты от накрутки:
- условия привязаны только к *засчитанным* счётчикам (is_counted=True);
- пороговые значения подобраны так, чтобы их нельзя было закрыть флудом
  (кулдаун 10 сек => 100 сообщений ≈ минимум 17 минут осмысленной активности);
- secret-ачивки выдаются только явными вызовами check(..., force_codes=[...]).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.db.models import (
    Achievement, AchievementCategory, AchievementRarity, ConditionType,
    User, UserAchievement,
)
from app.db.repositories import AchievementRepository


@dataclass(frozen=True)
class AchievementDef:
    code: str
    title: str
    description: str
    icon: str
    category: AchievementCategory
    condition_type: ConditionType
    condition_value: int
    reward_xp: int = 0
    reward_coins: int = 0
    is_hidden: bool = False
    rarity: AchievementRarity = AchievementRarity.common


# Справочник достижений (MVP-набор; пополняется на этапах 3–6)
ACHIEVEMENTS: list[AchievementDef] = [
    # 💬 Активность
    AchievementDef("msg_10", "Первые слова", "Написать 10 сообщений", "💬",
                   AchievementCategory.activity, ConditionType.messages_total, 10, 20, 5),
    AchievementDef("msg_100", "Болтун", "Написать 100 сообщений", "🗣",
                   AchievementCategory.activity, ConditionType.messages_total, 100, 100, 25),
    AchievementDef("msg_1000", "Голос чата", "Написать 1 000 сообщений", "📢",
                   AchievementCategory.activity, ConditionType.messages_total, 1000, 500, 100,
                   rarity=AchievementRarity.rare),
    AchievementDef("msg_10000", "Легенда чата", "Написать 10 000 сообщений", "👑",
                   AchievementCategory.activity, ConditionType.messages_total, 10000, 2500, 500,
                   rarity=AchievementRarity.legendary),
    AchievementDef("msg_50_day", "Марафон", "50 сообщений за один день", "🏃",
                   AchievementCategory.activity, ConditionType.messages_day, 50, 150, 30,
                   rarity=AchievementRarity.rare),
    # 🔥 Стрики
    AchievementDef("streak_7", "Неделя огня", "7 дней активности подряд", "🔥",
                   AchievementCategory.streak, ConditionType.streak_days, 7, 150, 40),
    AchievementDef("streak_30", "Месяц стали", "30 дней активности подряд", "🌋",
                   AchievementCategory.streak, ConditionType.streak_days, 30, 600, 150,
                   rarity=AchievementRarity.epic),
    AchievementDef("streak_365", "Год вместе", "365 дней активности подряд", "🎂",
                   AchievementCategory.streak, ConditionType.streak_days, 365, 5000, 1000,
                   rarity=AchievementRarity.legendary),
    # 😀 Реакции
    AchievementDef("react_give_50", "Щедрый палец", "Поставить 50 реакций", "👍",
                   AchievementCategory.reactions, ConditionType.reactions_given, 50, 80, 20),
    AchievementDef("react_give_500", "Реактор", "Поставить 500 реакций", "⚡",
                   AchievementCategory.reactions, ConditionType.reactions_given, 500, 400, 100,
                   rarity=AchievementRarity.rare),
    AchievementDef("react_get_100", "Любимец чата", "Получить 100 реакций", "💖",
                   AchievementCategory.reactions, ConditionType.reactions_received, 100, 250, 60,
                   rarity=AchievementRarity.rare),
    # 🐣 Тамагочи (реализуются на этапе 3)
    AchievementDef("pet_feed_100", "Заботливый хозяин", "Покормить питомца 100 раз", "🍎",
                   AchievementCategory.pet, ConditionType.pet_feeds, 100, 200, 50),
    AchievementDef("pet_level_5", "Вырастил!", "Довести питомца до 5 уровня", "🐉",
                   AchievementCategory.pet, ConditionType.pet_level, 5, 300, 80),
    AchievementDef("pet_walks_10", "Путешественник", "Отправить питомца на 10 прогулок", "🗺",
                   AchievementCategory.pet, ConditionType.pet_walks, 10, 150, 40),
    # 🏆 Социальные
    AchievementDef("top1_day", "Король дня", "Занять 1 место в топе за день", "🥇",
                   AchievementCategory.social, ConditionType.top1_day, 1, 300, 100,
                   rarity=AchievementRarity.epic),
    AchievementDef("invite_1", "Знакомый", "Пригласить друга", "🤝",
                   AchievementCategory.social, ConditionType.invites, 1, 50, 20),
    # 🎭 Секретные
    AchievementDef("night_owl", "Сова", "Написать сообщение в 3–5 ночи", "🦉",
                   AchievementCategory.secret, ConditionType.messages_total, 1, 100, 30,
                   is_hidden=True, rarity=AchievementRarity.epic),
    AchievementDef("first_steps", "Первый шаг", "Пройти онбординг", "🐣",
                   AchievementCategory.secret, ConditionType.messages_total, 1, 25, 10,
                   is_hidden=True),
    AchievementDef("walk_friend", "Новые знакомства", "Завести друга-питомца на прогулке", "💞",
                   AchievementCategory.secret, ConditionType.pet_walks, 1, 80, 25,
                   is_hidden=True, rarity=AchievementRarity.rare),
    # 🎮 Мини-игры (Этап 3.5)
    AchievementDef("games_won_10", "Игумен", "Выиграть 10 мини-игр", "🎮",
                   AchievementCategory.activity, ConditionType.games_won, 10, 120, 30,
                   rarity=AchievementRarity.rare),
]

_BY_CODE = {a.code: a for a in ACHIEVEMENTS}


async def seed_achievements(session: AsyncSession) -> int:
    """Идемпотентно загружает справочник достижений. Возвращает число созданных."""
    existing = {
        row for row in (await session.execute(select(Achievement.code))).scalars()
    }
    created = 0
    for i, a in enumerate(_BY_CODE.values(), start=1):
        if a.code in existing:
            continue
        session.add(Achievement(
            id=i, code=a.code, title=a.title, description=a.description, icon=a.icon,
            category=a.category, condition_type=a.condition_type,
            condition_value=a.condition_value, reward_xp=a.reward_xp,
            reward_coins=a.reward_coins, is_hidden=a.is_hidden, rarity=a.rarity,
        ))
        created += 1
    if created:
        await session.flush()
        logger.info("seeded {} achievements", created)
    return created


class AchievementService:
    """Проверяет прогресс и выдаёт награды.

    `check(user_id, counters)` принимает словарь вида
    {condition_type.value: current_value}. Для messages_total дополнительно
    можно передать 'messages_day' — маппится на условие messages_day.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = AchievementRepository(session)

    async def check(self, user_id: int, counters: dict[str, int],
                    force_codes: list[str] | None = None) -> list[Achievement]:
        """Возвращает список ТОЛЬКО ЧТО разблокированных достижений."""
        newly: list[Achievement] = []
        seen_ids: set[int] = set()
        ach_rows = (await self.session.execute(select(Achievement))).scalars().all()

        for a in ach_rows:
            if a.is_hidden and (force_codes is None or a.code not in force_codes):
                continue  # скрытые — только по прямому триггеру
            key = a.condition_type.value
            # спец-кейс: у условий messages_day значение берётся из counters["messages_day"]
            value = counters.get(key)
            if value is None and key == "messages_day":
                value = counters.get("messages_day_total")
            if value is None or value <= 0:
                continue
            unlocked_now = await self.repo.upsert_progress(user_id, a.id, int(value))
            if unlocked_now and a.id not in seen_ids:
                seen_ids.add(a.id)
                newly.append(a)

        if newly:
            await self._grant_rewards(user_id, newly)
        return newly

    async def unlock(self, achievement: Achievement, user_id: int) -> Achievement | None:
        """Прямая выдача по объекту достижения (админ-команды, секреты).

        Возвращает achievement, если оно открылось сейчас, иначе None.
        """
        unlocked_now = await self.repo.upsert_progress(
            user_id, achievement.id, achievement.condition_value)
        if unlocked_now:
            await self._grant_rewards(user_id, [achievement])
            return achievement
        return None

    async def unlock_by_code(self, user_id: int, code: str) -> Achievement | None:
        """Прямая выдача (секретки, ручные награды админом)."""
        a = (await self.session.execute(
            select(Achievement).where(Achievement.code == code)
        )).scalar_one_or_none()
        if a is None:
            return None
        already = (await self.session.execute(
            select(UserAchievement).where(
                UserAchievement.user_id == user_id,
                UserAchievement.achievement_id == a.id,
            )
        )).scalar_one_or_none()
        if already is not None and (already.unlocked_at is not None
                                    or already.progress >= a.condition_value):
            # уже открыто (или открылась ранее, но не зафиксирована) — помечаем и молча выходим
            if already.unlocked_at is None:
                already.unlocked_at = datetime.now(timezone.utc)
                await self.session.flush()
            return None
        unlocked_now = await self.repo.upsert_progress(user_id, a.id, a.condition_value)
        if unlocked_now:
            await self._grant_rewards(user_id, [a])
            return a
        return None

    async def _grant_rewards(self, user_id: int, achievements: list[Achievement]) -> None:
        xp = sum(a.reward_xp for a in achievements)
        coins = sum(a.reward_coins for a in achievements)
        if xp or coins:
            user = await self.session.get(User, user_id)
            if user:
                from app.utils.formatting import apply_xp
                user.level, user.xp, _ = apply_xp(user.level, user.xp, xp)
                user.coins += coins
                await self.session.flush()
        # уведомление в очередь (учитывает персональные настройки; флуд-защита —
        # batching в одном сообщении ниже). kind="achievement" — см. notifications.queue_notification
        try:
            from app.services.notifications import queue_notification
            lines = [f"{a.icon} <b>{a.title}</b> — {a.description}" for a in achievements]
            total_xp = sum(a.reward_xp for a in achievements)
            total_c = sum(a.reward_coins for a in achievements)
            text = ("🎉 <b>Новое достижение!</b>\n" + "\n".join(lines) +
                    f"\n\nНаграда: +{total_xp} XP · +{total_c} 🪙")
            await queue_notification(self.session, user_id, "achievement", text)
        except Exception as e:  # уведомления не должны ронять основной флоу
            logger.debug("achievement notify skipped: {}", e)
        for a in achievements:
            logger.info("🏆 user {} unlocked achievement {} (+{}xp +{}c)",
                        user_id, a.code, a.reward_xp, a.reward_coins)

    async def list_for_user(self, user_id: int) -> list[tuple[Achievement, UserAchievement | None]]:
        """Полный список ачивок с прогрессом пользователя (для экрана 🏆).

        Сортировка: сначала разблокированные (по дате), затем по редкости и
        порогу условия — так «Открыто: 2/20» на первой странице видно сразу.
        """
        rows = {r.achievement_id: r for r in await self.repo.get_progress_rows(user_id)}
        out = []
        for a in (await self.session.execute(select(Achievement))).scalars():
            if a.is_hidden and (ur := rows.get(a.id)) and not ur.unlocked_at:
                continue
            out.append((a, rows.get(a.id)))
        rarity_order = {"legendary": 0, "epic": 1, "rare": 2, "common": 3}
        out.sort(key=lambda t: (
            0 if (t[1] and t[1].unlocked_at) else 1,          # открытые — первыми
            rarity_order.get(getattr(t[0].rarity, "value", str(t[0].rarity)), 4),
            t[0].condition_value,                              # простые цели — раньше
            t[0].id,
        ))
        return out
