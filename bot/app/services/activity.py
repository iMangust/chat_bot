from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.config import get_settings
from app.db.models import ChatMessageLog, User
from app.db.repositories import ActivityRepository, UserRepository
from app.services.achievements import AchievementService
from app.utils.html_text import esc
from app.utils.redis import set_cooldown
from app.utils.local_time import db_bound, now as local_now

def _aware(dt: datetime) -> datetime:
    from app.utils.local_time import localize
    return localize(dt)

def _day(dt: datetime) -> datetime:
    dt = _aware(dt)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)

class ActivityService:
    def __init__(self, session: AsyncSession, bot=None) -> None:
        self.session = session
        self.bot = bot
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
        user = await self.users.get(user_id)
        if user is None:
            # Пользователь написал в группе, но ещё не проходил /start в
            # личке (или пишет от имени канала). Раньше такое сообщение
            # молча игнорировалось — самый частый баг «написал в группу,
            # а статистика не обновилась». Заводим запись автоматически.
            user = await self.users.get_or_create(user_id)
        if user.is_banned:
            return None
        if not user.onboarded and user.first_name:
            # Существующий пользователь (после /start) пишет от имени
            # канала/иначе и ещё не помечен онборднутым — добираем имя.
            # ВАЖНО: новым пользователям (без /start) onboarded здесь НЕ
            # ставим: иначе «Твоя статистика» показывает пустой профиль
            # («Питомец: ещё не заведён») вместо приглашения пройти /start.
            if not user.username and self.bot is not None:
                try:
                    chat = await self.bot.get_chat(user_id)
                    user.username = (chat.username or "")[:64] or None
                except Exception as exc:
                    logger.debug("channel author name fetch failed: {}", exc)
            await self.session.flush()

        now = local_now()
        length = len(text or "")

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

        await self._credit_referral(user)

        xp_gain = self.settings.xp_per_message
        if has_media:
            try:
                from app.handlers.tracker import MEDIA_XP_BONUS
                xp_gain += MEDIA_XP_BONUS.get(media_type or "", 1)
            except ImportError as exc:
                logger.debug("MEDIA_XP_BONUS import failed, fallback +1: {}", exc)
                xp_gain += 1
        if is_reply:
            xp_gain += 1
        coins_gain = self.settings.coins_per_message_cap

        self._update_streak(user, now)
        new_level, new_xp, leveled_to = self._apply_user_xp(user, xp_gain)
        user.xp = new_xp
        user.level = new_level
        user.coins += coins_gain
        user.messages_count += 1

        await self.session.flush()

        counters = await self._counters(user, now)
        unlocked = await self.achievements.check(user_id, counters)

        if leveled_to:
            logger.info("user {} leveled up to {}", user_id, leveled_to[-1])
        for ach in unlocked:
            logger.bind(notify=True).info("achievement {} unlocked for {}", ach.code, user_id)

        if leveled_to:
            from app.services.notifications import queue_levelup
            await queue_levelup(self.session, user_id, leveled_to)

        return entry

    async def _credit_referral(self, user: User) -> None:
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
        except Exception as exc:
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
        user = await self.users.get(user_id)
        if user is None:
            return None
        now = local_now()
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
        from app.db.models import ReactionLog
        user = await self.users.get(from_user)
        if user is None:
            # Реакцию поставил человек без записи в реестре (не проходил
            # /start или пишет от имени канала) — заводим его и засчитываем,
            # иначе «поставил реакцию, а статистика не обновилась».
            # onboarded НЕ ставим — онбординг только через /start.
            user = await self.users.get_or_create(from_user)
        if user.is_banned:
            return False
        day_start = _day(local_now())
        given_today = (await self.session.execute(
            select(func.count()).select_from(ReactionLog).where(
                ReactionLog.from_user == from_user,
                ReactionLog.to_user == to_user,
                ReactionLog.created_at >= db_bound(day_start),
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
        _, user.xp, leveled_to = self._apply_user_xp(user, 1)
        target = await self.users.get(to_user)
        if (target is not None and target.tg_id != from_user
                and not getattr(target, "is_bot", False)):
            # На реакции «для ботов» награду автору сообщения не начисляем —
            # счётчик «поставил» при этом уже увеличен выше.
            target.reactions_received += 1
            _, target.xp, _ = self._apply_user_xp(target, 2)
            t_counters = {"reactions_received": target.reactions_received}
            await self.achievements.check(to_user, t_counters)
        await self.session.flush()

        counters = {"reactions_given": user.reactions_given}
        unlocked = await self.achievements.check(from_user, counters)
        if leveled_to:
            from app.services.notifications import queue_levelup
            await queue_levelup(self.session, from_user, leveled_to)
        for ach in unlocked:
            logger.bind(notify=True).info("achievement {} unlocked for {}", ach.code, from_user)
        return True

    @staticmethod
    def _apply_user_xp(user: User, gained: int) -> tuple[int, int, list[int]]:
        from app.utils.formatting import apply_xp
        return apply_xp(user.level, user.xp, gained)

    @staticmethod
    def _update_streak(user: User, now: datetime) -> None:
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
            "invites": await self.users.get_stat(user.tg_id, "invites"),
            "games_won": await self.users.get_stat(user.tg_id, "games_won"),
            "top1_day": await self.users.get_stat(user.tg_id, "top1_day"),
        }
        pet = (await self.session.execute(
            select(Pet).where(Pet.user_id == user.tg_id, Pet.is_archived.is_(False))
        )).scalars().first()
        if pet is not None:
            from app.db.repositories import PetRepository
            pets = PetRepository(self.session)
            counters["pet_level"] = pet.level
            counters["pet_feeds"] = await pets.count_actions(pet.id, "feed")
            counters["pet_walks"] = (
                await pets.count_actions(pet.id, "walk_done")
                + await pets.count_actions(pet.id, "walk")
            )
        return counters

    async def personal_stats(self, tg_id: int) -> dict:
        now = local_now()
        repo = ActivityRepository(self.session)
        return {
            "total": await repo.messages_count(tg_id),
            "week": await repo.messages_count(tg_id, since=now - timedelta(days=7)),
            "day": await repo.messages_count(tg_id, since=_day(now)),
            "breakdown": await repo.media_breakdown(tg_id),
            "breakdown_week": await repo.media_breakdown(tg_id, since=now - timedelta(days=7)),
        }
