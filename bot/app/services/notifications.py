from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NotificationQueue, NotificationSetting, Pet, User
from app.services.tamagotchi import MOOD_TEXT, compute_mood
from app.utils.html_text import esc
from app.utils.local_time import now as local_now

async def queue_notification(session: AsyncSession, user_id: int, kind: str,
                             text: str, send_at: datetime | None = None) -> bool:
    ns = await session.get(NotificationSetting, user_id)
    if ns is not None:
        flag = {
            "pet": ns.pet_reminders,
            "streak": ns.streak_reminders,
            "achievement": ns.achievement_notifications,
            "levelup": ns.achievement_notifications,
            "daily": ns.daily_report,
        }.get(kind)
        if flag is False:
            return False
    session.add(NotificationQueue(user_id=user_id, kind=kind, text=text,
                                  send_at=send_at or local_now()))
    await session.flush()
    return True

async def has_recent(session: AsyncSession, user_id: int, kind: str,
                     within: timedelta) -> bool:
    cutoff = local_now() - within
    row = (await session.execute(
        select(NotificationQueue.id).where(
            NotificationQueue.user_id == user_id,
            NotificationQueue.kind == kind,
            NotificationQueue.send_at >= cutoff,
        ).limit(1)
    )).scalar_one_or_none()
    return row is not None

def _mood_phrase(mood: str) -> str:
    return MOOD_TEXT.get(mood, "Что-то приуныл")

async def build_pet_sad_text(pet: Pet) -> str:
    mood = compute_mood(pet)
    reason = _mood_phrase(mood)
    hints = {
        "hungry": "Кормёжка найдётся в 🎒 инвентаре или 🛒 магазине.",
        "sad": "Зайди в 🐾 Питомец → 🎾 Игры — 30 секунд и хвост снова трубой.",
        "sick": "Нужно 💊 лечение — загляни в магазин, это дёшево.",
    }
    hint = hints.get(mood, "")
    return f"🐾 <b>{esc(pet.name)}</b> {reason}\n{hint}".strip()

async def build_streak_warning(user: User) -> str:
    return (f"🔥 {esc(user.first_name)}, серия из <b>{user.streak_days}</b> дн. сгорит в полночь!\n"
            f"Напиши что-нибудь в чат — даже «спасибо» засчитается 🙂")

async def queue_levelup(session: AsyncSession, user_id: int, levels: list[int]) -> bool:
    if not levels:
        return False
    top = max(levels)
    text = f"🎉 Новый уровень: <b>{top}</b>! Так держать 🔥"
    return await queue_notification(session, user_id, "levelup", text)

async def build_daily_report(user: User, pet: Pet | None, stats_today: int,
                             rank: int | None) -> str:
    lines = [f"🌅 <b>Твой день, {esc(user.first_name)}</b>", ""]
    lines.append(f"💬 Сообщений сегодня: <b>{stats_today}</b>")
    if rank:
        lines.append(f"🏅 Место в дневном топе чата: <b>#{rank}</b>")
    if pet is not None:
        mood = compute_mood(pet)
        lines.append(f"🐾 {esc(pet.name)}: {_mood_phrase(mood)}")
    if user.streak_days:
        lines.append(f"🔥 Серия: {user.streak_days} дн. — не теряй её завтра!")
    lines.append("")
    lines.append("Загляни в бота — там кнопка «Продолжить день» 👇")
    return "\n".join(lines)
