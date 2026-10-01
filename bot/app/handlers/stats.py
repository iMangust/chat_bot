from __future__ import annotations

import html
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import UserRepository
from aiogram.filters import Command

from app.keyboards.inline import (achievements_list, back_to_main,
                                  main_menu, nav_row, top_tabs,
                                  with_nav)
from app.services.leaderboard import (overall_top, top_emotional, top_karma,
                                      top_levels, top_messages, top_pets,
                                      top_reactions, top_streaks)
from app.services.activity import ActivityService
from app.services.achievements import AchievementService
from app.utils.formatting import progress_bar, xp_needed_for_level
from app.utils.safe_edit import safe_edit_or_answer, answer_safe
from app.utils.local_time import now as local_now

router = Router(name="stats")

def _medal(i: int) -> str:
    return ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."

async def _top_lines(session: AsyncSession, period_label: str, since: datetime | None) -> list[str]:
    users = UserRepository(session)
    lines: list[str] = []
    if since is None:
        talkers = [(u, u.messages_count) for u in await users.top_by("messages_count", 10)]
    else:
        talkers = await users.top_period_messages(since, 10)
    if not talkers:
        lines.append("   пока пусто — будь первым! 💬")
    for i, (u, cnt) in enumerate(talkers, start=1):
        name = html.escape(u.first_name or "")
        lines.append(f"{_medal(i)} {name} — {cnt} сообщ. (ур. {u.level})")
    return lines

@router.callback_query(F.data == "menu:stats")
async def stats_screen(cb: CallbackQuery, session: AsyncSession) -> None:
    users = UserRepository(session)
    user = await users.get(cb.from_user.id)
    if user is None:
        await cb.answer("Сначала нажми /start", show_alert=True)
        return
    svc = ActivityService(session)
    st = await svc.personal_stats(user.tg_id)
    need = xp_needed_for_level(user.level)
    text = (
        f"📊 <b>Твоя статистика</b>\n\n"
        f"👤 {html.escape(user.first_name or '')} (@{html.escape(user.username or '—')})\n"
        f"🏅 Уровень {user.level} · {progress_bar(user.xp, need)} {user.xp}/{need} XP\n"
        f"🪙 Монеты: {user.coins}\n\n"
        f"💬 Сообщения: сегодня {st['day']} · неделя {st['week']} · всего {st['total']}\n"
        f"😀 Реакции: поставил {user.reactions_given} · получил {user.reactions_received}\n"
        f"🔥 Серия: {user.streak_days} дн. (рекорд {user.best_streak})\n"
        f"🐣 Питомец: {user.pet_name or 'ещё не заведён'}"
    )
    breakdown = _breakdown_text(st.get("breakdown") or {})
    if breakdown:
        text += f"\n\n🧩 Из чего состоят сообщения:\n{breakdown}"
    from app.keyboards.inline import InlineKeyboardBuilder
    _b = InlineKeyboardBuilder()
    _b.button(text="🏆 Достижения", callback_data="menu:ach")
    _b.button(text="🏅 Топы", callback_data="menu:top")
    _b.adjust(2)
    # «⬅️ Назад» — по истории переходов (обычно в главное меню);
    # «🏠 Меню» — гарантированный выход на главный экран.
    with_nav(_b, "stats", cb.message.chat.id if cb.message else None)
    await safe_edit_or_answer(cb.message, text, reply_markup=_b.as_markup())
    await cb.answer()

MEDIA_LABELS: list[tuple[str, str]] = [
    ("text", "💬 текст"), ("photo", "🖼 фото"), ("sticker", "🎴 стикеры"),
    ("voice", "🎤 голосовые"), ("video_note", "⭕️ кружки"), ("video", "🎬 видео"),
    ("animation", "✨ анимации"), ("audio", "🎵 музыка"), ("document", "📎 файлы"),
    ("poll", "📊 опросы"), ("other", "📦 прочее"),
    ("reply", "↩️ ответы"), ("mentions", "@ упоминания"),
]

def _breakdown_text(breakdown: dict[str, int], top: int = 8) -> str:
    total_msgs = sum(v for k, v in breakdown.items() if k not in ("reply", "mentions"))
    if not total_msgs:
        return ""
    lines = []
    for key, label in MEDIA_LABELS:
        cnt = breakdown.get(key)
        if not cnt:
            continue
        if key in ("reply", "mentions"):
            lines.append(f"   {label}: {cnt}")
        else:
            share = round(cnt * 100 / total_msgs)
            lines.append(f"   {label}: {cnt} ({share}%)")
        if len(lines) >= top:
            break
    return "\n".join(lines)

def _render_achievements(items, page: int = 0, page_size: int = 8) -> tuple[str, int, int]:
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    chunk = items[page * page_size:(page + 1) * page_size]

    unlocked_count = sum(1 for a, ur in items if ur and ur.unlocked_at)
    lines = ["🏆 <b>Достижения</b>\n", f"Открыто: {unlocked_count}/{len(items)}\n"]
    for a, ur in chunk:
        progress = ur.progress if ur else 0
        done = bool(ur and ur.unlocked_at)
        bar = progress_bar(min(progress, a.condition_value), a.condition_value, 8)
        mark = "✅" if done else "🔒"
        title = html.escape(a.title or "")
        desc = html.escape(a.description or "")
        icon = a.icon or "🏅"
        hidden = "🎭 " if a.is_hidden and not done else ""
        lines.append(
            f"{mark} {icon} <b>{hidden}{title}</b> — {desc}\n"
            f"   {bar} {min(progress, a.condition_value)}/{a.condition_value}"
        )
    return "\n".join(lines), page, total_pages

@router.callback_query(F.data.startswith("ach:noop"))
async def ach_noop(cb: CallbackQuery) -> None:
    await cb.answer()

@router.callback_query(F.data.startswith("top:noop"))
async def top_noop(cb: CallbackQuery) -> None:
    await cb.answer()

@router.callback_query(F.data == "menu:ach")
async def ach_screen(cb: CallbackQuery, session: AsyncSession, page: int = 0) -> None:
    svc = AchievementService(session)
    items = await svc.list_for_user(cb.from_user.id)
    text, page, total_pages = _render_achievements(items, page=page)
    await safe_edit_or_answer(cb.message, text,
                              reply_markup=achievements_list(
                                  items, page, total_pages,
                                  chat_id=cb.message.chat.id if cb.message else None))
    await cb.answer()

@router.callback_query(F.data.startswith("ach:page:"))
async def ach_page(cb: CallbackQuery, session: AsyncSession) -> None:
    try:
        page = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        page = 0
    await ach_screen(cb, session, page=page)

PERIODS = {"day": "📅 День", "week": "🗓 Неделя", "all": "♾ Всё время"}

def _since_for(period: str, now) -> datetime | None:
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
    return None

async def _top_section(session: AsyncSession, period: str, section: str) -> str:
    now = local_now()
    since = _since_for(period, now)
    lines = [f"🏅 <b>Топы чата · {PERIODS[period]} · {section_label(section)[0]} {section_label(section)[1]}</b>\n"]

    if section == "talk":
        talkers = await top_messages(session, since, 10)
        lines.append("💬 <b>Болтуны (сообщения)</b>")
        if not talkers:
            lines.append("   пока пусто — будь первым! 💬")
        for i, (u, c) in enumerate(talkers, start=1):
            me = " 👈 <i>это ты</i>" if u.tg_id == _TOP_CTX.get("me") else ""
            lines.append(f"{_medal(i)} {html.escape(u.first_name or '')} — {c} сообщ. (ур. {u.level}){me}")
    elif section == "react":
        reactors = await top_reactions(session, since, 10)
        lines.append("💖 <b>По полученным реакциям</b>")
        if not reactors:
            lines.append("   пока пусто 💖")
        for i, (u, c) in enumerate(reactors, start=1):
            me = " 👈 <i>это ты</i>" if u.tg_id == _TOP_CTX.get("me") else ""
            lines.append(f"{_medal(i)} {html.escape(u.first_name or '')} — {c}{me}")
    elif section == "emotional":
        emo = await top_emotional(session, since, 10)
        lines.append("🎭 <b>Самые эмоциональные (поставил + получил)</b>")
        if not emo:
            lines.append("   пока пусто 🎭")
        for i, (u, c) in enumerate(emo, start=1):
            me = " 👈 <i>это ты</i>" if u.tg_id == _TOP_CTX.get("me") else ""
            lines.append(f"{_medal(i)} {html.escape(u.first_name or '')} — {c} р. {progress_bar(c, emo[0][1], 6)}{me}")
    elif section == "karma":
        karma = await top_karma(session, since, 10)
        lines.append("💚 <b>Добряки (ответы + упоминания)</b>")
        if not karma:
            lines.append("   пока пусто 💚")
        for i, (u, c) in enumerate(karma, start=1):
            me = " 👈 <i>это ты</i>" if u.tg_id == _TOP_CTX.get("me") else ""
            lines.append(f"{_medal(i)} {html.escape(u.first_name or '')} — {c} взаим. {progress_bar(c, karma[0][1], 6)}{me}")
    elif section == "overall":
        overall = await overall_top(session, since, 10)
        lines.append("👑 <b>Общий топ (среднее место по всем номинациям)</b>")
        if not overall:
            lines.append("   пока пусто 👑")
        marks = {"talk": "💬", "react": "💖", "emotional": "🎭",
                 "streak": "🔥", "levels": "⭐", "karma": "💚"}
        for i, (u, score, per) in enumerate(overall, start=1):
            me = " 👈 <i>это ты</i>" if u.tg_id == _TOP_CTX.get("me") else ""
            detail = " ".join(f"{marks.get(s, '?')}{p}" for s, p in
                              sorted(per.items(), key=lambda kv: kv[1])[:4])
            lines.append(f"{_medal(i)} {html.escape(u.first_name or '')} — "
                         f"рейтинг {score:.1f} ({detail}){me}")
    elif section == "streak":
        streaks = await top_streaks(session, 10)
        lines.append("🔥 <b>Серии дней подряд</b>")
        if not streaks:
            lines.append("   пока пусто 🔥")
        for i, (u, days) in enumerate(streaks, start=1):
            me = " 👈 <i>это ты</i>" if u.tg_id == _TOP_CTX.get("me") else ""
            lines.append(f"{_medal(i)} {html.escape(u.first_name or '')} — {days} дн.{me}")
    elif section == "pets":
        pets = await top_pets(session, 10)
        lines.append("🐾 <b>Питомцы (по уровню)</b>")
        if not pets:
            lines.append("   пока пусто 🐾")
        for i, (p, owner) in enumerate(pets, start=1):
            lines.append(f"{_medal(i)} {html.escape(p.name)} (ур. {p.level}) · {html.escape(owner or '')}")
    else:
        levels = await top_levels(session, 10)
        lines.append("⭐ <b>Уровни игроков</b>")
        if not levels:
            lines.append("   пока пусто ⭐")
        for i, (u, lvl) in enumerate(levels, start=1):
            me = " 👈 <i>это ты</i>" if u.tg_id == _TOP_CTX.get("me") else ""
            lines.append(f"{_medal(i)} {html.escape(u.first_name or '')} — ур. {lvl}{me}")

    lines.append("\n<i>/award — итоги прошлой недели с призами 🎁</i>")
    return "\n".join(lines)

TOP_SECTIONS: list[tuple[str, str]] = [
    ("overall", "👑 Общий"), ("talk", "💬 Болтуны"), ("react", "💖 Реакции"),
    ("emotional", "🎭 Эмоциональные"), ("karma", "💚 Добряки"),
    ("streak", "🔥 Серии"), ("pets", "🐾 Питомцы"), ("levels", "⭐ Уровни"),
]

def section_label(section: str) -> tuple[str, str]:
    for key, label in TOP_SECTIONS:
        if key == section:
            emoji, _, title = label.partition(" ")
            return emoji, title
    return "🏅", "Топы"

_TOP_CTX: dict[str, int] = {}

def _parse_top_cb(data: str) -> tuple[str, str]:
    parts = data.split(":")
    period, section = "week", "talk"
    if len(parts) >= 2 and parts[1] in PERIODS:
        period = parts[1]
    if len(parts) >= 3 and any(k == parts[2] for k, _ in TOP_SECTIONS):
        section = parts[2]
    return period, section

@router.callback_query(F.data == "menu:top")
@router.callback_query(F.data.startswith("top:"))
async def top_screen(cb: CallbackQuery, session: AsyncSession) -> None:
    period, section = _parse_top_cb(cb.data)
    _TOP_CTX["me"] = cb.from_user.id
    text = await _top_section(session, period, section)
    await safe_edit_or_answer(cb.message, text,
                              reply_markup=top_tabs(period, section,
                                                    chat_id=cb.message.chat.id if cb.message else None))
    await cb.answer()

@router.message(Command("top"), F.chat.type == "private")
async def cmd_top(message: Message, session: AsyncSession) -> None:
    _TOP_CTX["me"] = message.from_user.id
    text = await _top_section(session, "week", "talk")
    await answer_safe(message, text, reply_markup=top_tabs("week", "talk"))

@router.message(Command("ach", "achievements"), F.chat.type == "private")
async def cmd_ach(message: Message, session: AsyncSession) -> None:
    svc = AchievementService(session)
    items = await svc.list_for_user(message.from_user.id)
    text, page, total_pages = _render_achievements(items, page=0)
    await answer_safe(message, text,
                      reply_markup=achievements_list(items, page, total_pages))

@router.message(Command("stats"), F.chat.type == "private")
async def cmd_stats(message: Message, session: AsyncSession) -> None:
    users = UserRepository(session)
    user = await users.get(message.from_user.id)
    if user is None:
        await message.answer("Сначала нажми /start 🐣", reply_markup=main_menu())
        return
    svc = ActivityService(session)
    st = await svc.personal_stats(user.tg_id)
    need = xp_needed_for_level(user.level)
    text = (
        f"📊 <b>Твоя статистика</b>\n\n"
        f"👤 {html.escape(user.first_name or '')} (@{html.escape(user.username or '—')})\n"
        f"🏅 Уровень {user.level} · {progress_bar(user.xp, need)} {user.xp}/{need} XP\n"
        f"🪙 Монеты: {user.coins}\n\n"
        f"💬 Сообщения: сегодня {st['day']} · неделя {st['week']} · всего {st['total']}\n"
        f"😀 Реакции: поставил {user.reactions_given} · получил {user.reactions_received}\n"
        f"🔥 Серия: {user.streak_days} дн. (рекорд {user.best_streak})\n"
        f"🐣 Питомец: {html.escape(user.pet_name or 'ещё не заведён')}"
    )
    breakdown = _breakdown_text(st.get("breakdown") or {})
    if breakdown:
        text += f"\n\n🧩 Из чего состоят сообщения:\n{breakdown}"
    await answer_safe(message, text, reply_markup=back_to_main())
