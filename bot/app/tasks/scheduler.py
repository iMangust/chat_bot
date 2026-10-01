from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from loguru import logger

from app.config import get_settings
from app.db.models import (NotificationQueue, NotificationSetting, Pet, User)
from app.db.repositories import ActivityRepository
from app.db.session import session_factory
from app.services.leaderboard import snapshot_weekly
from app.services.notifications import (build_daily_report, build_pet_sad_text,
                                        build_streak_warning, has_recent,
                                        queue_notification)
from app.services.pet_social import list_friends
from app.services.tamagotchi import TamagotchiService, compute_mood
from app.utils.redis import acquire_lock, release_lock
from app.utils.local_time import KAMCHATKA_TZ, localize, now as local_now

async def decay_all_pets(bot: Bot) -> None:
    if not await acquire_lock("decay", ttl_sec=60 * 25):
        return
    try:
        try:
            from app.services.weather import kamchatka_weather, apply_weather_to_pet
            await kamchatka_weather()
        except Exception as exc:
            logger.debug("weather refresh in decay tick failed: {}", exc)
            apply_weather_to_pet = None
        async with session_factory() as session:
            pets = list((await session.execute(
                select(Pet).where(Pet.is_archived.is_(False)))).scalars())
            svc = TamagotchiService(session)
            warned = 0
            weather_events = 0
            min_h = get_settings().pet_warning_min_hours
            for pet in pets:
                changed = await svc.apply_decay(pet)
                if apply_weather_to_pet is not None:
                    try:
                        wline = apply_weather_to_pet(pet)
                        if wline:
                            changed = True
                            weather_events += 1
                            logger.debug("weather event pet={}: {}", pet.id, wline.replace("\n", " | "))
                    except Exception as exc:
                        logger.debug("weather effect skipped for pet {}: {}", pet.id, exc)
                friends = await list_friends(session, pet.id)
                if friends:
                    day_key = "friend_bonus_day"
                    today = local_now().date().isoformat()
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
            logger.info("decay tick done: {} pets, {} new warnings, {} weather events",
                        len(pets), warned, weather_events)
    finally:
        await release_lock("decay")

def compute_mood_reason(mood: str) -> str:
    return {
        "sad": "грустит без тебя… Зайди поиграй! 🎾",
        "sick": "заболел! Нужно лечение 💊",
        "hungry": "голодный! Покорми меня 🍎",
    }.get(mood, "хочет внимания")

async def flush_notifications(bot: Bot) -> None:
    if not await acquire_lock("notify_flush", ttl_sec=50):
        return
    try:
        now = local_now()
        async with session_factory() as session:
            rows = list((await session.execute(
                select(NotificationQueue).where(
                    NotificationQueue.sent.is_(False),
                    NotificationQueue.send_at <= now,
                ).limit(30)
            )).scalars())
            sent = 0
            for n in rows:
                # награды из админки дублируем в последний чат пользователя —
                # если бот заблокирован в ЛС, приз всё равно будет виден
                chat_fallback = None
                if n.kind == "reward":
                    from app.db.models import ChatMessageLog, User as _U
                    chat_fallback = (await session.execute(
                        select(ChatMessageLog.chat_id)
                        .where(ChatMessageLog.user_id == n.user_id)
                        .order_by(ChatMessageLog.created_at.desc()).limit(1)
                    )).scalar_one_or_none()
                    uname = ""
                    if chat_fallback:
                        uname = (await session.execute(
                            select(_U.first_name).where(_U.tg_id == n.user_id)
                        )).scalar_one_or_none() or str(n.user_id)
                # клавиатура, прикреплённая к уведомлению (например,
                # «Продолжить день» в дневном отчёте)
                kb = None
                if getattr(n, "payload_json", None):
                    try:
                        from aiogram.types import InlineKeyboardMarkup
                        kb = InlineKeyboardMarkup.model_validate_json(n.payload_json)
                    except Exception as exc:  # noqa: BLE001 — без кнопок лучше, чем мимо
                        logger.debug("notification {} kb parse failed: {}", n.id, exc)
                try:
                    await bot.send_message(n.user_id, n.text, parse_mode="HTML",
                                           reply_markup=kb)
                    n.sent = True
                    sent += 1
                except TelegramAPIError as exc:
                    logger.debug("notification {} to {} dropped: {}",
                                 n.id, n.user_id, str(exc)[:120])
                    if chat_fallback:
                        try:
                            await bot.send_message(
                                chat_fallback,
                                f"🎁 <b>{esc(uname)}</b>, вам начислено:\n{n.text}",
                                parse_mode="HTML")
                        except TelegramAPIError:
                            pass
                    n.sent = True
            await session.commit()
            if rows:
                logger.info("notifications flushed: {}/{}", sent, len(rows))
    finally:
        await release_lock("notify_flush")

async def check_streak_expiry(bot: Bot) -> None:
    if not await acquire_lock("streak_check", ttl_sec=3600):
        return
    try:
        now = local_now()
        yesterday = (now - timedelta(days=1)).date()
        async with session_factory() as session:
            users = list((await session.execute(
                select(User).where(User.streak_days > 0, User.onboarded.is_(True))
            )).scalars())
            expired = 0
            for u in users:
                last_date = None
                if u.last_active_date is not None:
                    # День стрика — КАМЧАТСКИЙ календарный день; naive-метки из
                    # старых БД считаем локальными (см. localize).
                    last_date = localize(u.last_active_date).date()
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
    off = set((await session.execute(
        select(NotificationSetting.user_id).where(getattr(NotificationSetting, flag).is_(False))
    )).scalars())
    return off

async def daily_reports(bot: Bot) -> None:
    if not await acquire_lock("daily_report", ttl_sec=3000):
        return
    try:
        async with session_factory() as session:
            users = list((await session.execute(
                select(User).where(User.onboarded.is_(True))
            )).scalars())
            off = await _flagged_users(session, "daily_report")
            repo = ActivityRepository(session)
            now = local_now()
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            sent = 0
            for u in users:
                if int(u.tg_id) in off:
                    continue
                if await has_recent(session, int(u.tg_id), "daily", timedelta(hours=20)):
                    continue
                stats_today = await repo.messages_count(int(u.tg_id), since=day_start)
                if stats_today == 0:
                    continue
                pet = (await session.execute(
                    select(Pet).where(Pet.user_id == u.tg_id, Pet.is_archived.is_(False))
                )).scalars().first()
                # прирост XP/монет за сегодня — по правилам начисления
                # активности (xp_per_message / coins_per_message_cap)
                st = get_settings()
                xp_gained = stats_today * st.xp_per_message
                coins_earned = min(stats_today, st.coins_per_message_cap * 10)
                text = await build_daily_report(
                    u, pet, stats_today, rank=None,
                    xp_gained=xp_gained, new_level=False,
                    coins_earned=coins_earned)
                # «Продолжить день» — кнопка под самим отчётом; если её не
                # прикрепить, обещание в тексте становится недостижимым.
                from app.keyboards.inline import daily_report_kb
                ok = await queue_notification(session, int(u.tg_id), "daily", text,
                                              reply_markup=daily_report_kb())
                if ok:
                    sent += 1
            await session.commit()
            logger.info("daily reports queued: {}", sent)
    finally:
        await release_lock("daily_report")



async def evening_streak_warnings(bot: Bot) -> None:
    if not await acquire_lock("streak_warn", ttl_sec=3000):
        return
    try:
        async with session_factory() as session:
            now = local_now()
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
    if not await acquire_lock("weekly_lb", ttl_sec=3000):
        return
    try:
        async with session_factory() as session:
            await snapshot_weekly(session)
    except Exception as e:
        logger.error("weekly leaderboard failed: {}", e)
    finally:
        await release_lock("weekly_lb")

async def weekly_arena_finish(bot: Bot) -> None:
    if not await acquire_lock("weekly_arena", ttl_sec=3000):
        return
    try:
        from app.services.pet_duels import finish_week
        async with session_factory() as session:
            awarded = await finish_week(session)
            if awarded:
                logger.info("🏟 arena week closed, prizes distributed")
    except Exception as e:
        logger.error("weekly arena finish failed: {}", e)
    finally:
        await release_lock("weekly_arena")

async def scan_channel_members(bot: Bot) -> None:
    st = get_settings()
    from app.middlewares.gate import required_chats, serviceable_chats
    if not serviceable_chats():
        return
    if not await acquire_lock("channel_scan", ttl_sec=max(60, st.channel_scan_minutes * 60 - 30)):
        return
    try:
        from app.db.repositories import SubscriberRepository
        async with session_factory() as session:
            repo = SubscriberRepository(session)
            known_users = await repo.distinct_user_count()
            known_rows = await repo.count()
            chats = serviceable_chats()
            probe_chat = st.channel_chat_id or (chats[0][0] if chats else None)
            total = None
            if probe_chat is not None:
                try:
                    total = await bot.get_chat_member_count(probe_chat)
                except Exception as e:
                    logger.debug("channel member count unavailable: {}", e)
            if total is not None and total > known_users:
                full_hours = max(0, int(getattr(st, "mtproto_full_scan_hours", 6) or 0))
                if full_hours > 0:
                    hint = (f"полный MTProto-скан включён (раз в {full_hours} ч) — "
                            f"следующий занесёт недостающих; срочно: /syncnow rescan")
                else:
                    hint = ("включите MTPROTO_FULL_SCAN_HOURS (или /syncnow rescan) — "
                            "Bot API не отдаёт список «молчунов», chat_member-апдейты "
                            "их не ловят")
                logger.info("📢 subscribers drift: api={} db={} человек "
                            "(строк в базе: {}) — {}",
                            total, known_users, known_rows, hint)
    except Exception as e:
        logger.error("channel scan failed: {}", e)
    finally:
        await release_lock("channel_scan")

async def mtproto_delta_sync(bot: Bot | None = None) -> None:
    if not await acquire_lock("mtproto_sync", ttl_sec=60 * 50):
        return
    try:
        from app.services.mtproto_sync import (mtproto_configured,
                                               sync_subscribers)
        if not mtproto_configured():
            return
        full = False
        st = get_settings()
        full_hours = max(0, int(getattr(st, "mtproto_full_scan_hours", 6) or 0))
        if full_hours > 0:
            from app.utils.redis import remember_for
            full = await remember_for("mtproto_full_scan", full_hours * 3600)
        if full:
            from app.services.mtproto_sync import full_rescan_subscribers
            res = await asyncio.wait_for(full_rescan_subscribers(), timeout=280)
            logger.info("MTProto FULL scan: {}", res)
        else:
            res = await asyncio.wait_for(sync_subscribers(first_run=False), timeout=280)
            logger.info("MTProto delta sync: {}", res)
    except asyncio.TimeoutError:
        logger.warning("MTProto delta sync: таймаут (сеть/флудконтроль?)")
    except RuntimeError:
        pass
    except Exception as exc:
        logger.info("MTProto delta sync пропущен: {}: {}",
                    type(exc).__name__, str(exc)[:200])
    finally:
        await release_lock("mtproto_sync")

async def weather_updater(bot: Bot) -> None:
    if not get_settings().weather_real_enabled:
        return
    try:
        from app.services.weather import background_refresh
        info = await background_refresh()
        if info is not None:
            logger.info("🌦️ weather updater: кэш обновлён ({}°C, {:.1f} м/с)",
                        info.get("temperature", "?"),
                        (float(info.get("wind") or 0) / 3.6))
    except Exception as exc:
        logger.warning("weather updater failed: {}: {}", type(exc).__name__, exc)

def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    # Планировщик живёт в КАМЧАТСКОЙ зоне: в расписании указываются привычные
    # локальные часы (DAILY_REPORT_HOUR и т.п.), а не UTC.
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo("Asia/Kamchatka")
    except Exception:
        tz = KAMCHATKA_TZ
    sched = AsyncIOScheduler(timezone=tz)
    # job_defaults: coalesce+max_history — если несколько запусков пропускаются
    # (например, тяжёлый MTProto-скан заблокировал цикл событий на минуту),
    # APScheduler не считает каждый пропущенный тик ошибкой и не пишет
    # «Run time of job ... was missed by ...» в консоль. misfire_grace_time —
    # допустимое запаздывание, в пределах которого задача выполняется сразу
    # после разблокировки вместо пропуска.
    sched.configure(job_defaults={"coalesce": True, "max_instances": 1,
                                  "misfire_grace_time": 300})
    sched.add_job(decay_all_pets, "interval", minutes=30, args=[bot],
                  max_instances=1, coalesce=True, id="decay")
    sched.add_job(flush_notifications, "interval", minutes=1, args=[bot],
                  max_instances=1, coalesce=True, id="notify")
    sched.add_job(scan_channel_members, "interval",
                  minutes=get_settings().channel_scan_minutes, args=[bot],
                  max_instances=1, coalesce=True, id="chanscan")
    # 00:15 по Камчатке: к этому моменту локальный день гарантированно сменился
    sched.add_job(check_streak_expiry, "cron", hour=0, minute=15, args=[bot],
                  id="streaks")
    st = get_settings()
    sched.add_job(daily_reports, "cron", hour=st.daily_report_hour, minute=5,
                  args=[bot], id="daily", max_instances=1, coalesce=True)
    sched.add_job(evening_streak_warnings, "cron", hour=st.evening_reminder_hour,
                  minute=40, args=[bot], id="streakwarn", max_instances=1, coalesce=True)
    sched.add_job(weekly_leaderboard, "cron", day_of_week="mon", hour=8, minute=30,
                  args=[bot], id="weeklylb", max_instances=1, coalesce=True)
    sched.add_job(weekly_arena_finish, "cron", day_of_week="mon", hour=8, minute=40,
                  args=[bot], id="weeklyarena", max_instances=1, coalesce=True)
    if st.mtproto_sync_minutes > 0 and st.telegram_api_id and st.telegram_api_hash:
        sched.add_job(mtproto_delta_sync, "interval", minutes=st.mtproto_sync_minutes,
                      args=[bot],
                      id="mtproto_sync", max_instances=1, coalesce=True,
                      next_run_time=local_now() + timedelta(seconds=90))
    weather_hours = max(0.5, float(getattr(st, "weather_refresh_hours", 3.0) or 3.0))
    sched.add_job(weather_updater, "interval", hours=weather_hours, args=[bot],
                  id="weather", max_instances=1, coalesce=True,
                  next_run_time=local_now() + timedelta(seconds=5))
    return sched
