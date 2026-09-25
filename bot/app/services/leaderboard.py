"""Лидерборды (Этап 5): топы за день/неделю/всё время + еженедельные награды.

Топы считаются на лету из users + chat_messages_log/reactions_log.
Раз в неделю (воскресенье 00:30 UTC) планировщик делает снапшот в
leaderboards_snapshot и выдаёт призёрам монеты/XP + уведомление.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (ChatMessageLog, LeaderboardSnapshot, Pet, ReactionLog,
                           User, UserStat, utcnow)
from app.services.notifications import queue_notification
from app.utils.html_text import esc as _esc

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
WEEKLY_PRIZES = {1: 500, 2: 250, 3: 100}  # монеты за 1/2/3 место недели


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def top_messages(session: AsyncSession, since: datetime | None,
                       limit: int = 10) -> list[tuple[User, int]]:
    """Топ болтунов. since=None — за всё время (денорм. счётчик users)."""
    if since is None:
        rows = list((await session.execute(
            select(User).order_by(User.messages_count.desc()).limit(limit)
        )).scalars())
        return [(u, u.messages_count) for u in rows if u.messages_count > 0]
    cnt = func.count(ChatMessageLog.id).label("c")
    q = (select(User, cnt)
         .join(ChatMessageLog, ChatMessageLog.user_id == User.tg_id)
         .where(ChatMessageLog.created_at >= since, ChatMessageLog.is_counted.is_(True))
         .group_by(User.tg_id).order_by(cnt.desc()).limit(limit))
    return [(u, c) for u, c in (await session.execute(q)).all() if c > 0]


async def top_reactions(session: AsyncSession, since: datetime | None,
                        limit: int = 10) -> list[tuple[User, int]]:
    """Топ по полученным реакциям."""
    if since is None:
        rows = list((await session.execute(
            select(User).order_by(User.reactions_received.desc()).limit(limit)
        )).scalars())
        return [(u, u.reactions_received) for u in rows if u.reactions_received > 0]
    cnt = func.count(ReactionLog.id).label("c")
    q = (select(User, cnt)
         .join(ReactionLog, ReactionLog.to_user == User.tg_id)
         .where(ReactionLog.created_at >= since)
         .group_by(User.tg_id).order_by(cnt.desc()).limit(limit))
    return [(u, c) for u, c in (await session.execute(q)).all() if c > 0]


async def top_reactions_given(session: AsyncSession, since: datetime | None,
                              limit: int = 10) -> list[tuple[User, int]]:
    """Топ по ПОСТАВЛЕННЫМ реакциям (v1.4.7, номинация «Самый эмоциональный»)."""
    if since is None:
        rows = list((await session.execute(
            select(User).order_by(User.reactions_given.desc()).limit(limit)
        )).scalars())
        return [(u, u.reactions_given) for u in rows if u.reactions_given > 0]
    cnt = func.count(ReactionLog.id).label("c")
    q = (select(User, cnt)
         .join(ReactionLog, ReactionLog.from_user == User.tg_id)
         .where(ReactionLog.created_at >= since)
         .group_by(User.tg_id).order_by(cnt.desc()).limit(limit))
    return [(u, c) for u, c in (await session.execute(q)).all() if c > 0]


async def top_karma(session: AsyncSession, since: datetime | None,
                    limit: int = 10) -> list[tuple[User, int]]:
    """«Добрый» топ (v1.4.7): забота об общении — ответы + упоминания."""
    cond = [ChatMessageLog.is_counted.is_(True)]
    if since is not None:
        cond.append(ChatMessageLog.created_at >= since)
    karma = (func.coalesce(func.sum(ChatMessageLog.is_reply.cast(Integer)), 0)
             + func.coalesce(func.sum(ChatMessageLog.mentions_count), 0)).label("k")
    q = (select(User, karma)
         .join(ChatMessageLog, ChatMessageLog.user_id == User.tg_id)
         .where(*cond)
         .group_by(User.tg_id).order_by(karma.desc()).limit(limit))
    return [(u, int(k)) for u, k in (await session.execute(q)).all() if k and int(k) > 0]


async def top_emotional(session: AsyncSession, since: datetime | None,
                        limit: int = 10) -> list[tuple[User, int]]:
    """«Самый эмоциональный» (v1.4.7): сумма поставленных + полученных реакций."""
    given = await top_reactions_given(session, since, 50)
    received = dict(await top_reactions(session, since, 50))
    total: dict[int, tuple[User, int]] = {}
    for u, c in given:
        total[u.tg_id] = (u, c + received.get(u.tg_id, 0))
    for u, c in received.items():
        if u.tg_id not in total:
            total[u.tg_id] = (u, c)
    ranked = sorted(total.values(), key=lambda x: -x[1])[:limit]
    return [(u, c) for u, c in ranked if c > 0]


async def overall_top(session: AsyncSession, since: datetime | None,
                      limit: int = 10) -> list[tuple[User, float, dict[str, int]]]:
    """Усреднённый топ (v1.4.7): сумма мест по всем номинациям, меньше — лучше.

    Возвращает [(user, avg_place, {section: place})]; в расчёт берутся только
    участники хотя бы одного локального топа (остальные не ранжированы).
    """
    sections: dict[str, list[tuple[User, int]]] = {
        "talk": await top_messages(session, since, 20),
        "react": await top_reactions(session, since, 20),
        "emotional": await top_emotional(session, since, 20),
        "streak": await top_streaks(session, 20),
        "levels": await top_levels(session, 20),
        "karma": await top_karma(session, since, 20),
    }
    places: dict[int, dict[str, int]] = {}
    users: dict[int, User] = {}
    n = max(len(v) for v in sections.values()) if any(sections.values()) else 0
    for name, rows in sections.items():
        for i, (u, _c) in enumerate(rows, start=1):
            users.setdefault(u.tg_id, u)
            places.setdefault(u.tg_id, {})[name] = i
    out: list[tuple[User, float, dict[str, int]]] = []
    for uid, per in places.items():
        # среднее по секциям, где пользователь засветился; бонус за широту:
        # отсутствие в секции = место n+1 (штраф, чтобы «всё понемногу»
        # выигрывало у «чемпион в одном»)
        score = sum(per.get(s, n + 1) for s in sections) / len(sections)
        out.append((users[uid], round(score, 2), per))
    out.sort(key=lambda x: x[1])
    return out[:limit]


async def top_levels(session: AsyncSession, limit: int = 10) -> list[tuple[User, int]]:
    users = list((await session.execute(
        select(User).where(User.level > 0)
        .order_by(User.level.desc(), User.xp.desc()).limit(limit)
    )).scalars())
    return [(u, u.level) for u in users]


async def top_streaks(session: AsyncSession, limit: int = 10) -> list[tuple[User, int]]:
    users = list((await session.execute(
        select(User).where(User.streak_days > 0)
        .order_by(User.streak_days.desc()).limit(limit)
    )).scalars())
    return [(u, u.streak_days) for u in users]


async def top_pets(session: AsyncSession, limit: int = 10) -> list[tuple[Pet, str]]:
    rows = (await session.execute(
        select(Pet, User.first_name).join(User, User.tg_id == Pet.user_id)
        .where(Pet.is_archived.is_(False))
        .order_by(Pet.level.desc(), Pet.xp.desc()).limit(limit)
    )).all()
    return [(p, name) for p, name in rows]


async def build_weekly_payload(session: AsyncSession, week_start: datetime) -> dict:
    """Собираем данные недельного снапшота (JSON-safe)."""
    talkers = await top_messages(session, week_start, 10)
    reactors = await top_reactions(session, week_start, 10)
    pets = await top_pets(session, 10)
    return {
        "week_start": week_start.isoformat(),
        "messages": [[int(u.tg_id), u.first_name, c] for u, c in talkers],
        "reactions": [[int(u.tg_id), u.first_name, c] for u, c in reactors],
        "pets": [[p.id, p.name, p.level, owner] for p, owner in pets],
    }


def leaderboard_text(payload: dict) -> str:
    """Человеческое представление снапшота (для экрана /award и пост-отчёта)."""
    lines = ["🏆 <b>Итоги прошлой недели</b>\n"]
    if payload.get("messages"):
        lines.append("💬 Болтуны:")
        for i, (_tid, name, c) in enumerate(payload["messages"][:5], start=1):
            lines.append(f"  {MEDALS.get(i, f'{i}.')} {_esc(name)} — {c} сообщ.")
    if payload.get("reactions"):
        lines.append("\n💖 Любимцы чата (реакции):")
        for i, (_tid, name, c) in enumerate(payload["reactions"][:5], start=1):
            lines.append(f"  {MEDALS.get(i, f'{i}.')} {_esc(name)} — {c} реакций")
    if payload.get("pets"):
        lines.append("\n🐾 Питомцы:")
        for i, (_pid, pname, lvl, owner) in enumerate(payload["pets"][:5], start=1):
            lines.append(f"  {MEDALS.get(i, f'{i}.')} {_esc(pname)} (ур. {lvl}) · {_esc(owner)}")
    lines.append("\n🪙 Призёрам 💬-топа начислены монеты: 500 / 250 / 100!")
    return "\n".join(lines)


async def snapshot_weekly(session: AsyncSession,
                          now: datetime | None = None) -> dict | None:
    """Еженедельная задача: снапшот + награды топ-3 болтунов недели.

    Возвращает payload снапшота или None, если эта неделя уже отмечена
    (идемпотентность через UserStat key='last_week_award').
    """
    now = now or utcnow()
    # понедельник текущей недели (UTC); считаем итоги предыдущей
    this_monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    week_start = this_monday - timedelta(days=7)
    week_end = this_monday

    marker_key = f"last_week_award:{week_start.date().isoformat()}"
    # Идемпотентность: маркер храним в снапшоте (category='weekly_award'),
    # а НЕ в UserStat(user_id=0) — user_id=0 нарушает FK users.tg_id на MySQL.
    marker = (await session.execute(
        select(LeaderboardSnapshot.id).where(
            LeaderboardSnapshot.period == "week",
            LeaderboardSnapshot.category == marker_key,
        ).limit(1)
    )).scalar_one_or_none()
    if marker is not None:
        snap = (await session.execute(
            select(LeaderboardSnapshot).where(
                LeaderboardSnapshot.period == "week",
                LeaderboardSnapshot.category == "weekly_summary",
            ).order_by(LeaderboardSnapshot.id.desc()).limit(1)
        )).scalars().first()
        return snap.data if snap else None

    payload = await build_weekly_payload(session, week_start)
    session.add(LeaderboardSnapshot(period="week", category="weekly_summary",
                                    data=payload))
    session.add(LeaderboardSnapshot(period="week", category=marker_key,
                                    data=[]))

    # награды топ-3 болтунов недели + счётчик для ачивки «Король дня»
    talkers = await top_messages(session, week_start, 3)
    for place, (user, _cnt) in enumerate(talkers, start=1):
        prize = WEEKLY_PRIZES.get(place, 0)
        if prize:
            user.coins += prize
            user.xp += prize // 2
            await queue_notification(
                session, int(user.tg_id), "info",
                f"🎉 Ты #{place} в недельном топе болтунов! Приз: 🪙 {prize} монет.",
            )
    # «Король дня» — дневной топ на момент снапшота
    day_top = await top_messages(session, now - timedelta(days=1), 1)
    if day_top:
        winner = day_top[0][0]
        st = (await session.execute(
            select(UserStat).where(UserStat.user_id == winner.tg_id,
                                   UserStat.key == "top1_day")
        )).scalar_one_or_none()
        if st is None:
            session.add(UserStat(user_id=int(winner.tg_id), key="top1_day", value=1))
        else:
            st.value += 1
    await session.commit()
    logger_note = f"weekly snapshot stored ({len(payload['messages'])} talkers)"
    from loguru import logger
    logger.info(logger_note)
    return payload
