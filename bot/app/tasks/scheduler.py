"""Фоновые задачи (APScheduler): деградация питомцев, уведомления, стрики.

Масштабируемость: каждая задача обёрнута в Redis-lock — можно запускать
несколько копий бота (worкеров), задача выполнится только на одном.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from loguru import logger

from app.db.models import NotificationQueue, Pet, User
from app.db.session import session_factory
from app.services.tamagotchi import TamagotchiService, compute_mood
from app.utils.redis import acquire_lock, release_lock


async def decay_all_pets(bot: Bot) -> None:
    """Каждые 30 минут: применяем деградацию и генерируем «питомец скучает»."""
    if not await acquire_lock("decay", ttl_sec=60 * 25):
        return
    try:
        async with session_factory() as session:
            pets = list((await session.execute(select(Pet))).scalars())
            svc = TamagotchiService(session)
            warned = 0
            for pet in pets:
                changed = await svc.apply_decay(pet)
                mood = compute_mood(pet)
                # очередь уведомлений вместо мгновенного пуша (защита от флуда)
                if mood in ("sad", "sick", "hungry") and changed:
                    last = (await session.execute(
                        select(NotificationQueue.send_at)
                        .where(
                            NotificationQueue.user_id == pet.user_id,
                            NotificationQueue.kind == "pet",
                            NotificationQueue.sent.is_(False),
                        )
                        .limit(1)
                    )).scalar_one_or_none()
                    if last is None or last < datetime.now(timezone.utc) - timedelta(hours=6):
                        session.add(NotificationQueue(
                            user_id=pet.user_id, kind="pet",
                            text=f"🐾 <b>{pet.name}</b> {compute_mood_reason(mood)}",
                        ))
                        warned += 1
            await session.commit()
            logger.info("decay tick done: {} pets, {} new warnings", len(pets), warned)
    finally:
        await release_lock("decay")


def compute_mood_reason(mood: str) -> str:
    return {
        "sad": "грустит без тебя… Зайди поиграй! 🎾",
        "sick": "заболел! Нужно лечение 💊",
        "hungry": "голодный! Покорми меня 🍎",
    }.get(mood, "хочет внимания")


async def flush_notifications(bot: Bot) -> None:
    """Каждую минуту: отправляем накопленные уведомления в ЛС."""
    if not await acquire_lock("notify_flush", ttl_sec=50):
        return
    try:
        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            rows = list((await session.execute(
                select(NotificationQueue).where(
                    NotificationQueue.sent.is_(False),
                    NotificationQueue.send_at <= now,
                ).limit(30)  # не больше 30 в тик — бережём rate-limit Telegram
            )).scalars())
            sent = 0
            for n in rows:
                try:
                    await bot.send_message(n.user_id, n.text)
                    n.sent = True
                    sent += 1
                except Exception:
                    # юзер заблокировал бота — помечаем отправленным, чтобы не копить мусор
                    n.sent = True
            await session.commit()
            if rows:
                logger.info("notifications flushed: {}/{}", sent, len(rows))
    finally:
        await release_lock("notify_flush")


async def check_streak_expiry(bot: Bot) -> None:
    """Раз в сутки (00:15 UTC): сгоревшие стрики → предупреждение."""
    if not await acquire_lock("streak_check", ttl_sec=3600):
        return
    try:
        now = datetime.now(timezone.utc)
        yesterday = (now - timedelta(days=1)).date()
        async with session_factory() as session:
            users = list((await session.execute(
                select(User).where(User.streak_days > 0, User.onboarded.is_(True))
            )).scalars())
            expired = 0
            for u in users:
                # MySQL DATETIME -> naive; приводим к UTC перед сравнением дат
                last_date = None
                if u.last_active_date is not None:
                    la = u.last_active_date
                    if la.tzinfo is None:
                        la = la.replace(tzinfo=timezone.utc)
                    last_date = la.astimezone(timezone.utc).date()
                if last_date != yesterday:
                    if u.streak_days > 0:
                        expired += 1
                        session.add(NotificationQueue(
                            user_id=u.tg_id, kind="streak",
                            text="🔥 Твоя серия дней сгорела. Начни новую — напиши что-нибудь в чат!",
                        ))
                    u.streak_days = 0
            await session.commit()
            logger.info("streak check: {} streaks expired", expired)
    finally:
        await release_lock("streak_check")


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(decay_all_pets, "interval", minutes=30, args=[bot],
                  max_instances=1, coalesce=True, id="decay")
    sched.add_job(flush_notifications, "interval", minutes=1, args=[bot],
                  max_instances=1, coalesce=True, id="notify")
    sched.add_job(check_streak_expiry, "cron", hour=0, minute=15, args=[bot],
                  id="streaks")
    return sched
