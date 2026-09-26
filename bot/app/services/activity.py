"""Сервис активности: антифрод-фильтры, XP, стрики.

Правила зачёта сообщения (все проверяются до записи в лог):
1. Не ЛС, не канал (только группы/супергруппы из tracked_chat_ids или любые группы).
2. Не бот, не забанен, прошёл /start (onboarded).
3. Не команда (/...), не сервисное сообщение (вступление, закреп и т.п.).
4. Длина текста >= min_message_length ИЛИ медиа (стикер/фото/голосовое считаются).
5. Кулдаун activity_cooldown_sec между засчитанными сообщениями одного юзера (Redis SETNX).
6. Дедупликация по message_id (уникальность обеспечивается логикой редактирования —
   edited-событие не вызывает этот метод повторно для зачёта).

Все сообщения пишутся в chat_messages_log (включая незаачтённые, с skip_reason) —
это даёт данные для анализа накрутки.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.config import get_settings
from app.db.models import ChatMessageLog, User
from app.db.repositories import ActivityRepository, UserRepository
from app.services.achievements import AchievementService
from app.utils.html_text import esc
from app.utils.redis import set_cooldown


def _aware(dt: datetime) -> datetime:
    """Приводит datetime из БД к aware-UTC (MySQL/DATETIME возвращают naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _day(dt: datetime) -> datetime:
    dt = _aware(dt)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


class ActivityService:
    def __init__(self, session: AsyncSession, bot=None) -> None:
        self.session = session
        self.bot = bot  # опционально: для мгновенных уведомлений о ачивках/локапах в ЛС
        self.users = UserRepository(session)
        self.activity = ActivityRepository(session)
        self.achievements = AchievementService(session)
        self.settings = get_settings()

    async def process_group_message(
        self,
        *,
        user_id: int,
        chat_id: int,
        message_id: int,
        text: str | None,
        has_media: bool,
        media_type: str | None,
        is_reply: bool,
        mentions_count: int,
        is_command: bool,
    ) -> ChatMessageLog | None:
        """Главная точка входа для события Message из группы/канала.

        Возвращает запись лога (counted или нет) либо None, если юзер ещё
        не зарегистрирован (не жмём /start — не трогаем БД лишний раз).
        """
        user = await self.users.get(user_id)
        if user is None or user.is_banned:
            return None
        # Автор поста в канале — сам канал (sender_chat): у него нет ЛС и он
        # не проходил онбординг. Регистрируем «виртуального» автора, чтобы
        # канальная активность начислялась на статистику канала.
        if not user.onboarded:
            if chat_id == user_id:
                user.onboarded = True
                if not user.first_name:
                    try:
                        chat = await self.bot.get_chat(user_id)
                        user.first_name = (chat.title or chat.username or "")[:128]
                    except Exception as exc:  # бот не видит канал — оставляем как есть
                        logger.debug("channel author name fetch failed: {}", exc)
                await self.session.flush()
            else:
                return None

        now = datetime.now(timezone.utc)
        length = len(text or "")

        # --- фильтры (логируем всё для антифрод-аналитики) ---
        skip_reason: str | None = None
        if is_command:
            skip_reason = "command"
        elif length < self.settings.min_message_length and not has_media:
            skip_reason = "short"
        else:
            ok = await set_cooldown(f"msg:{user_id}", self.settings.activity_cooldown_sec)
            if not ok:
                skip_reason = "cooldown"

        entry = ChatMessageLog(
            user_id=user_id, chat_id=chat_id, message_id=message_id,
            length=length, has_media=has_media, media_type=media_type,
            is_reply=is_reply, mentions_count=mentions_count,
            is_counted=skip_reason is None, skip_reason=skip_reason,
            created_at=now,
        )
        await self.activity.log_message(entry)

        if skip_reason is not None:
            return entry

        # --- засчёт: XP + монеты + стрик ---
        await self._credit_referral(user)

        xp_gain = self.settings.xp_per_message
        # небольшие бонусы за «социальные» форматы: голосовые/кружки дороже текста,
        # reply — взаимодействие. Тип медиа определяет бонус (см. tracker.MEDIA_XP_BONUS).
        if has_media:
            try:
                from app.handlers.tracker import MEDIA_XP_BONUS
                xp_gain += MEDIA_XP_BONUS.get(media_type or "", 1)
            except ImportError as exc:  # трекер не импортируется — дефолтный бонус
                logger.debug("MEDIA_XP_BONUS import failed, fallback +1: {}", exc)
                xp_gain += 1
        if is_reply:
            xp_gain += 1
        coins_gain = self.settings.coins_per_message_cap

        # стрик обновляем ДО начисления XP, чтобы ачивки видели актуальное значение
        self._update_streak(user, now)
        new_level, new_xp, leveled_to = self._apply_user_xp(user, xp_gain)
        user.xp = new_xp
        user.level = new_level
        user.coins += coins_gain
        user.messages_count += 1  # денормализованный счётчик для топов

        await self.session.flush()

        # --- пересчёт достижений по активности ---
        counters = await self._counters(user, now)
        unlocked = await self.achievements.check(user_id, counters)

        if leveled_to:
            logger.info("user {} leveled up to {}", user_id, leveled_to[-1])
        for ach in unlocked:
            logger.bind(notify=True).info("achievement {} unlocked for {}", ach.code, user_id)

        # v1.5.9: НИКАКИХ мгновенных DM за каждое сообщение/стикер. Вся фоновая
        # активность (XP, монеты, стрики, пересчёт ачивок) проходит молча.
        # Мгновенное уведомление — только секретная ачивка «Сова» (редкое
        # событие, см. handlers/tracker.py). Важные события уходят в очередь
        # NotificationQueue и доставляются планировщиком раз в минуту:
        #  - левелапы — сюда;
        #  - достижения — через AchievementService._grant_rewards -> queue_notification.
        if leveled_to:
            from app.services.notifications import queue_levelup
            await queue_levelup(self.session, user_id, leveled_to)

        return entry

    async def _credit_referral(self, user: User) -> None:
        """Разовая награда пригласившему за первую засчитанную активность новичка.

        Реферальная связка создаётся в /start по deep-link `invite_<tg_id>`
        (см. handlers/start.py). Здесь она «монетизируется»: бонд происходит
        только после реального действия приглашённого — это отсекает накрутку
        пустыми регистрациями. Флаг `_ref_credited` живёт в JSON-колонке
        settings_extra, поэтому не требует новой миграции.
        """
        if not user.referrer_id:
            return
        extra = dict(user.settings_extra or {})
        if extra.get("_ref_credited"):
            return
        inviter = await self.users.get(user.referrer_id)
        if inviter is None or inviter.is_banned:
            return
        gained = await self.users.bump_stat(inviter.tg_id, "invites", 1)
        await self.users.add_xp_coins(inviter.tg_id, xp=30,
                                      coins=self.settings.invite_reward_coins)
        await self.achievements.check(inviter.tg_id, {"invites": gained})
        extra["_ref_credited"] = True
        user.settings_extra = extra
        await self.session.flush()
        try:
            await self.bot.send_message(
                inviter.tg_id,
                f"🤝 Твоя ссылка сработала! <b>{esc(user.first_name or 'друг')}</b> "
                f"проявил активность в чате.\n"
                f"Награда: +{self.settings.invite_reward_coins} 🪙 и 30 XP. "
                f"Приглашено всего: {gained}.",
              parse_mode="HTML",
            )
        except Exception as exc:  # ЛС закрыты — не критично
            logger.debug("referral notify failed for {}: {}", inviter.tg_id, exc)
        logger.info("referral credited: inviter={} invitee={}", inviter.tg_id, user.tg_id)

    async def log_message_only(
        self,
        *,
        user_id: int,
        chat_id: int,
        message_id: int,
        text: str | None,
        has_media: bool,
        media_type: str | None,
        is_reply: bool = False,
        mentions_count: int = 0,
    ) -> ChatMessageLog | None:
        """Записать сообщение в чат-лог БЕЗ начисления XP (v1.5.19).

        Нужно для событий, которые Bot API не отдаёт боту, но видит
        MTProto-аккаунт: посты канала (бот не получает published-сообщения
        каналов) и реакции на них. Без записи в лог реакция на пост канала
        не зачтётся — автор сообщения будет неизвестен.

        Идемпотентно по (chat_id, message_id); незаархивированных/незарегистрированных
        юзеров пропускаем (не плодим записи с внешними id).
        """
        user = await self.users.get(user_id)
        if user is None:
            return None
        now = datetime.now(timezone.utc)
        entry = ChatMessageLog(
            user_id=user_id, chat_id=chat_id, message_id=message_id,
            length=len(text or ""), has_media=has_media, media_type=media_type,
            is_reply=is_reply, mentions_count=mentions_count,
            is_counted=False, skip_reason="mtproto_backfill",
            created_at=now,
        )
        await self.activity.log_message(entry)
        return entry

    async def process_reaction(
        self, *, from_user: int, to_user: int, chat_id: int,
        message_id: int, emoji: str,
    ) -> bool:
        """Зачёт реакции. True — если новая (не дубль от того же юзера).

        XP за реакцию получают обе стороны: автор реакции (социальный вклад)
        и получатель (признание). Антифрод: взаимный «накрутас» из двух
        аккаунтов режется дневным лимитом зачёта реакций на одного
        получателя (reactions_cap_per_day).
        """
        from app.db.models import ReactionLog
        user = await self.users.get(from_user)
        if user is None or user.is_banned or not user.onboarded:
            return False
        # суточный лимит засчитанных реакций от одного фейкера к одному цели
        day_start = _day(datetime.now(timezone.utc))
        given_today = (await self.session.execute(
            select(func.count()).select_from(ReactionLog).where(
                ReactionLog.from_user == from_user,
                ReactionLog.to_user == to_user,
                ReactionLog.created_at >= day_start,
                ReactionLog.is_counted.is_(True),
            )
        )).scalar_one()
        counted = given_today < self.settings.reactions_cap_per_day

        entry = ReactionLog(from_user=from_user, to_user=to_user, chat_id=chat_id,
                            message_id=message_id, emoji=emoji, is_counted=counted)
        is_new = await self.activity.log_reaction(entry)
        if not is_new or not counted:
            return False

        user.reactions_given += 1
        # небольшой XP за социальное действие (без монет — только за сообщения)
        _, user.xp, leveled_to = self._apply_user_xp(user, 1)
        target = await self.users.get(to_user)
        if target is not None and target.tg_id != from_user:
            target.reactions_received += 1
            _, target.xp, _ = self._apply_user_xp(target, 2)
            t_counters = {"reactions_received": target.reactions_received}
            await self.achievements.check(to_user, t_counters)
        await self.session.flush()

        counters = {"reactions_given": user.reactions_given}
        unlocked = await self.achievements.check(from_user, counters)
        # v1.5.9: никаких мгновенных DM за реакции — левелап уходит в очередь
        # (доставится планировщиком раз в минуту), достижения уже в очереди
        # через AchievementService._grant_rewards.
        if leveled_to:
            from app.services.notifications import queue_levelup
            await queue_levelup(self.session, from_user, leveled_to)
        for ach in unlocked:
            logger.bind(notify=True).info("achievement {} unlocked for {}", ach.code, from_user)
        return True

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------
    @staticmethod
    def _apply_user_xp(user: User, gained: int) -> tuple[int, int, list[int]]:
        from app.utils.formatting import apply_xp
        return apply_xp(user.level, user.xp, gained)

    @staticmethod
    def _update_streak(user: User, now: datetime) -> None:
        """Стрик по календарным дням (UTC). Пропуск дня >1 обнуляет серию.

        Edge case: планировщик сжигает стрик в 00:15 UTC, но «вчерашний» день
        формально ещё вчера для last_active_date — если стрик был обнулён после
        последней активности (streak==0), считаем это продолжением вчерашней
        серии, а не новой единицей.
        """
        today = _day(now)
        last = _day(user.last_active_date) if user.last_active_date else None
        if last == today:
            return
        if last is not None and (today - last) == timedelta(days=1):
            user.streak_days = max(user.streak_days, 1) + 1
        else:
            user.streak_days = 1
        user.best_streak = max(user.best_streak, user.streak_days)
        user.last_active_date = now

    async def _counters(self, user: User, now: datetime) -> dict[str, int]:
        from app.db.models import Pet
        day_start = _day(now)
        week_start = day_start - timedelta(days=6)
        total = await self.activity.messages_count(user.tg_id)
        week = await self.activity.messages_count(user.tg_id, since=week_start)
        counters = {
            "messages_total": total,
            "messages_week": week,
            "messages_day": await self.activity.messages_count(user.tg_id, since=day_start),
            "streak_days": user.streak_days,
            "level": user.level,
            "coins_earned": user.coins,
            "reactions_given": user.reactions_given,
            "reactions_received": user.reactions_received,
            # пользовательские счётчики (invites и т.п.)
            "invites": await self.users.get_stat(user.tg_id, "invites"),
            "games_won": await self.users.get_stat(user.tg_id, "games_won"),
            "top1_day": await self.users.get_stat(user.tg_id, "top1_day"),
        }
        # питомец: уровень + агрегаты действий из pet_actions_log (для ачивок тамагочи)
        pet = (await self.session.execute(
            select(Pet).where(Pet.user_id == user.tg_id, Pet.is_archived.is_(False))
        )).scalars().first()
        if pet is not None:
            from app.db.repositories import PetRepository
            pets = PetRepository(self.session)
            counters["pet_level"] = pet.level
            counters["pet_feeds"] = await pets.count_actions(pet.id, "feed")
            counters["pet_walks"] = await pets.count_actions(pet.id, "walk_done")
        return counters

    async def personal_stats(self, tg_id: int) -> dict:
        """Данные для /stats и карточки профиля.

        v1.4.7: добавлена разбивка по типам сообщений (breakdown) — статистика
        теперь различает текст/фото/стикеры/голос/кружки/reply/упоминания.
        """
        now = datetime.now(timezone.utc)
        repo = ActivityRepository(self.session)
        return {
            "total": await repo.messages_count(tg_id),
            "week": await repo.messages_count(tg_id, since=now - timedelta(days=7)),
            "day": await repo.messages_count(tg_id, since=_day(now)),
            "breakdown": await repo.media_breakdown(tg_id),
            "breakdown_week": await repo.media_breakdown(tg_id, since=now - timedelta(days=7)),
        }
