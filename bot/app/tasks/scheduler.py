"""Фоновые задачи (APScheduler): деградация питомцев, уведомления, стрики.

Масштабируемость: каждая задача обёрнута в Redis-lock — можно запускать
несколько копий бота (worкеров), задача выполнится только на одном.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from loguru import logger

from app.config import get_settings
from app.db.models import (NotificationQueue, NotificationSetting, Pet, User,
                           utcnow)
from app.db.repositories import ActivityRepository
from app.db.session import session_factory
from app.services.leaderboard import snapshot_weekly
from app.services.notifications import (build_daily_report, build_pet_sad_text,
                                        build_streak_warning, has_recent,
                                        queue_notification)
from app.services.pet_social import list_friends
from app.services.tamagotchi import TamagotchiService, compute_mood
from app.utils.redis import acquire_lock, release_lock


async def decay_all_pets(bot: Bot) -> None:
    """Каждые 30 минут: деградация статов, бонус друзей, «питомец скучает»."""
    if not await acquire_lock("decay", ttl_sec=60 * 25):
        return
    try:
        async with session_factory() as session:
            pets = list((await session.execute(
                select(Pet).where(Pet.is_archived.is_(False)))).scalars())
            svc = TamagotchiService(session)
            warned = 0
            min_h = get_settings().pet_warning_min_hours
            for pet in pets:
                changed = await svc.apply_decay(pet)
                # пассивный бонус дружбы: +1 счастье за друга в сутки
                friends = await list_friends(session, pet.id)
                if friends:
                    day_key = "friend_bonus_day"
                    today = utcnow().date().isoformat()
                    extra = pet.settings_extra or {}
                    if extra.get(day_key) != today:
                        pet.happiness = min(100.0, pet.happiness + len(friends))
                        pet.settings_extra = {**extra, day_key: today}
                mood = compute_mood(pet)
                if mood in ("sad", "sick", "hungry") and changed:
                    recent = await has_recent(session, int(pet.user_id), "pet",
                                              timedelta(hours=min_h))
                    if not recent:
                        text = await build_pet_sad_text(pet)
                        await queue_notification(session, int(pet.user_id), "pet", text)
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
                    await bot.send_message(n.user_id, n.text, parse_mode="HTML")
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




async def _flagged_users(session, flag: str) -> set[int]:
    """tg_id юзеров, у которых настройка flag включена (или записи нет — по умолчанию вкл)."""
    off = set((await session.execute(
        select(NotificationSetting.user_id).where(getattr(NotificationSetting, flag).is_(False))
    )).scalars())
    return off


async def daily_reports(bot: Bot) -> None:
    """Ежедневный отчёт активности (daily_report_hour_utc)."""
    if not await acquire_lock("daily_report", ttl_sec=3000):
        return
    try:
        async with session_factory() as session:
            users = list((await session.execute(
                select(User).where(User.onboarded.is_(True))
            )).scalars())
            off = await _flagged_users(session, "daily_report")
            repo = ActivityRepository(session)
            now = utcnow()
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            sent = 0
            for u in users:
                if int(u.tg_id) in off:
                    continue
                if await has_recent(session, int(u.tg_id), "daily", timedelta(hours=20)):
                    continue
                stats_today = await repo.messages_count(int(u.tg_id), since=day_start)
                if stats_today == 0:
                    continue  # не было активности — не дёргаем лишний раз
                pet = (await session.execute(
                    select(Pet).where(Pet.user_id == u.tg_id, Pet.is_archived.is_(False))
                )).scalars().first()
                text = await build_daily_report(u, pet, stats_today, rank=None)
                await queue_notification(session, int(u.tg_id), "daily", text)
                sent += 1
            await session.commit()
            logger.info("daily reports queued: {}", sent)
    finally:
        await release_lock("daily_report")


async def evening_streak_warnings(bot: Bot) -> None:
    """Вечером: у кого стрик >0 и сегодня ещё не писал — «серия сгорит»."""
    if not await acquire_lock("streak_warn", ttl_sec=3000):
        return
    try:
        async with session_factory() as session:
            now = utcnow()
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            users = list((await session.execute(
                select(User).where(User.streak_days > 0, User.onboarded.is_(True))
            )).scalars())
            off = await _flagged_users(session, "streak_reminders")
            repo = ActivityRepository(session)
            queued = 0
            for u in users:
                if int(u.tg_id) in off:
                    continue
                today_msgs = await repo.messages_count(int(u.tg_id), since=day_start)
                if today_msgs:
                    continue
                if await has_recent(session, int(u.tg_id), "streak", timedelta(hours=12)):
                    continue
                text = await build_streak_warning(u)
                await queue_notification(session, int(u.tg_id), "streak", text)
                queued += 1
            await session.commit()
            logger.info("streak warnings queued: {}", queued)
    finally:
        await release_lock("streak_warn")


async def weekly_leaderboard(bot: Bot) -> None:
    """Понедельник 00:30 UTC: снапшот топа + призы."""
    if not await acquire_lock("weekly_lb", ttl_sec=3000):
        return
    try:
        async with session_factory() as session:
            await snapshot_weekly(session)
    except Exception as e:  # noqa: BLE001
        logger.error("weekly leaderboard failed: {}", e)
    finally:
        await release_lock("weekly_lb")


async def weekly_arena_finish(bot: Bot) -> None:
    """Понедельник 00:40 UTC: призы топ-3 недельной арены питомцев.

    Идемпотентно по маркеру в LeaderboardSnapshot — повторный запуск (рестарт
    процесса, два воркера с Redis-lock) не выдаст призы дважды.
    """
    if not await acquire_lock("weekly_arena", ttl_sec=3000):
        return
    try:
        from app.services.pet_duels import finish_week
        async with session_factory() as session:
            awarded = await finish_week(session)
            if awarded:
                logger.info("🏟 arena week closed, prizes distributed")
    except Exception as e:  # noqa: BLE001
        logger.error("weekly arena finish failed: {}", e)
    finally:
        await release_lock("weekly_arena")


async def scan_channel_members(bot: Bot) -> None:
    """Фоновый скан подписчиков канала (v1.5.1).

    Bot API не отдаёт список участников канала и события «подписка» без
    админ-прав, поэтому используем дешёвый get_chat_member_count: если счётчик
    вырос относительно нашей базы — значит кто-то подписался молча. Точных id
    новых подписчиков API не даёт, поэтому приветствие доставляется по нашим
    сигналам: chat_member-апдейт (нужны админ-права) или первое сообщение
    пользователя. Здесь же чистим «зависшие» pending-приветствия (например,
    человек сначала закрыл ЛС, а потом открыл).
    """
    st = get_settings()
    if not st.welcome_channel_enabled:
        return
    from app.middlewares.gate import required_chats
    if not required_chats():
        return
    if not await acquire_lock("channel_scan", ttl_sec=max(60, st.channel_scan_minutes * 60 - 30)):
        return
    try:
        from app.handlers.welcome import welcome_pending_subscribers
        from app.db.repositories import SubscriberRepository
        async with session_factory() as session:
            # догоняем приветствия, которые не удалось отправить раньше
            sent = await welcome_pending_subscribers(bot, session, limit=5)
            known = await SubscriberRepository(session).count()
            # счётчик берём по первому настроенному чату (channel_chat_id или
            # первый из TRACKED_CHAT_IDS) — раньше скан молча не работал без него
            probe_chat = st.channel_chat_id or required_chats()[0][0]
            try:
                total = await bot.get_chat_member_count(probe_chat)
            except Exception as e:  # noqa: BLE001 — бот не админ/нет доступа к каналу
                logger.debug("channel member count unavailable: {}", e)
                total = None
            if total is not None and total > known:
                logger.info("📢 subscribers drift: api={} db={} "
                            "(новых молчунов можно поймать только через "
                            "chat_member-апдейты — выдай боту права админа канала)",
                            total, known)
            elif sent:
                logger.info("channel welcome flush: {} DM sent", sent)
    except Exception as e:  # noqa: BLE001
        logger.error("channel scan failed: {}", e)
    finally:
        await release_lock("channel_scan")


async def mtproto_delta_sync(bot: Bot | None = None) -> None:
    """v1.5.12 (фикс v1.5.15): периодическая MTProto-дельта (полный Telegram API).

    Bot API не отдаёт список участников канала — Telethon закрывает этот
    пробел: раз в MTPROTO_SYNC_MINUTES тянем участников обязательных чатов
    и добавляем только «свежих» (id больше максимального известного).
    Задача создаётся только если MTProto настроен; ошибки не валят бота.
    ВАЖНО: aiogram 3.x НЕ имеет Bot.get_current() — бот передаётся планировщиком
    (как args=[bot]); fallback на dispatch-контекст оставлен для совместимости.
    """
    if not await acquire_lock("mtproto_sync", ttl_sec=60 * 50):
        return
    try:
        from app.services.mtproto_sync import (mtproto_configured,
                                               sync_subscribers)
        if not mtproto_configured():
            return
        res = await asyncio.wait_for(sync_subscribers(first_run=False), timeout=280)
        logger.info("MTProto delta sync: {}", res)
        # v1.5.16: приветствия раздаём через dispatch_welcomes() — он создаёт
        # собственный Bot-контекст и не зависит от «get_current»/инициализации
        # session в task'е планировщика (в логах было: AttributeError
        # type object 'Bot' has no attribute 'get_current').
        if res.get("added"):
            try:
                from app.services.mtproto_sync import dispatch_welcomes
                n = await dispatch_welcomes(limit=20)
                if n:
                    logger.info("MTProto sync: отправлено приветствий: {}", n)
            except Exception as exc:  # noqa: BLE001
                logger.warning("MTProto welcome flush failed: {} ({}) "
                               "(разойдутся на ближайшем скане)",
                               type(exc).__name__, str(exc)[:150])
    except asyncio.TimeoutError:
        logger.warning("MTProto delta sync: таймаут (сеть/флудконтроль?)")
    except RuntimeError:  # вне контекста бота — разойдутся по следующему тикам
        pass
    except Exception as exc:  # noqa: BLE001 — без сессии/кредов тихо живём на Bot API
        logger.info("MTProto delta sync пропущен: {}: {}",
                    type(exc).__name__, str(exc)[:200])
    finally:
        await release_lock("mtproto_sync")


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(decay_all_pets, "interval", minutes=30, args=[bot],
                  max_instances=1, coalesce=True, id="decay")
    sched.add_job(flush_notifications, "interval", minutes=1, args=[bot],
                  max_instances=1, coalesce=True, id="notify")
    sched.add_job(scan_channel_members, "interval",
                  minutes=get_settings().channel_scan_minutes, args=[bot],
                  max_instances=1, coalesce=True, id="chanscan")
    sched.add_job(check_streak_expiry, "cron", hour=0, minute=15, args=[bot],
                  id="streaks")
    st = get_settings()
    sched.add_job(daily_reports, "cron", hour=st.daily_report_hour_utc, minute=5,
                  args=[bot], id="daily", max_instances=1, coalesce=True)
    sched.add_job(evening_streak_warnings, "cron", hour=st.evening_reminder_hour_utc,
                  minute=40, args=[bot], id="streakwarn", max_instances=1, coalesce=True)
    sched.add_job(weekly_leaderboard, "cron", day_of_week="mon", hour=0, minute=30,
                  args=[bot], id="weeklylb", max_instances=1, coalesce=True)
    sched.add_job(weekly_arena_finish, "cron", day_of_week="mon", hour=0, minute=40,
                  args=[bot], id="weeklyarena", max_instances=1, coalesce=True)
    # v1.5.12: MTProto-дельта (полный API), только если ключи заданы
    if st.mtproto_sync_minutes > 0 and st.telegram_api_id and st.telegram_api_hash:
        sched.add_job(mtproto_delta_sync, "interval", minutes=st.mtproto_sync_minutes,
                      args=[bot],
                      id="mtproto_sync", max_instances=1, coalesce=True,
                      next_run_time=datetime.now(timezone.utc) + timedelta(seconds=90))
    return sched
