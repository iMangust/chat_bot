"""Экраны статистики, достижений и топов."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import ActivityRepository, UserRepository
from aiogram.filters import Command
from sqlalchemy import select

from app.db.models import ChatMessageLog
from app.keyboards.inline import (achievements_list, back_to_main, main_menu,
                                  top_tabs)
from app.services.leaderboard import (top_levels, top_messages, top_pets,
                                      top_reactions, top_streaks)
from app.services.activity import ActivityService
from app.services.achievements import AchievementService
from app.utils.formatting import progress_bar, xp_needed_for_level

router = Router(name="stats")


def _medal(i: int) -> str:
    return ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."


async def _top_lines(session: AsyncSession, period_label: str, since: datetime | None) -> list[str]:
    """Строки топа болтунов за период (since=None — за всё время)."""
    users = UserRepository(session)
    lines: list[str] = []
    if since is None:
        talkers = [(u, u.messages_count) for u in await users.top_by("messages_count", 10)]
    else:
        talkers = await users.top_period_messages(since, 10)
    if not talkers:
        lines.append("   пока пусто — будь первым! 💬")
    for i, (u, cnt) in enumerate(talkers, start=1):
        lines.append(f"{_medal(i)} {u.first_name} — {cnt} сообщ. (ур. {u.level})")
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
        f"👤 {user.first_name} (@{user.username or '—'})\n"
        f"🏅 Уровень {user.level} · {progress_bar(user.xp, need)} {user.xp}/{need} XP\n"
        f"🪙 Монеты: {user.coins}\n\n"
        f"💬 Сообщения: сегодня {st['day']} · неделя {st['week']} · всего {st['total']}\n"
        f"😀 Реакции: поставил {user.reactions_given} · получил {user.reactions_received}\n"
        f"🔥 Серия: {user.streak_days} дн. (рекорд {user.best_streak})\n"
        f"🐣 Питомец: {user.pet_name or 'ещё не заведён'}"
    )
    await cb.message.edit_text(text, reply_markup=back_to_main())
    await cb.answer()


@router.callback_query(F.data == "menu:ach")
async def ach_screen(cb: CallbackQuery, session: AsyncSession, page: int = 0) -> None:
    svc = AchievementService(session)
    items = await svc.list_for_user(cb.from_user.id)
    page_size = 8
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    chunk = items[page * page_size:(page + 1) * page_size]

    lines = ["🏆 <b>Достижения</b>\n"]
    unlocked_count = sum(1 for a, ur in items if ur and ur.unlocked_at)
    lines.append(f"Открыто: {unlocked_count}/{len(items)}\n")
    for a, ur in chunk:
        progress = ur.progress if ur else 0
        done = bool(ur and ur.unlocked_at)
        bar = progress_bar(min(progress, a.condition_value), a.condition_value, 8)
        mark = "✅" if done else "🔒"
        hidden = "🎭 " if a.is_hidden and not done else ""
        lines.append(
            f"{mark} {a.icon} <b>{hidden}{a.title}</b> — {a.description}\n"
            f"   {bar} {min(progress, a.condition_value)}/{a.condition_value}"
        )
    await cb.message.edit_text("\n".join(lines),
                               reply_markup=achievements_list(items, page, page_size))
    await cb.answer()


@router.callback_query(F.data.startswith("ach:page:"))
async def ach_page(cb: CallbackQuery, session: AsyncSession) -> None:
    await ach_screen(cb, session, page=int(cb.data.split(":")[2]))


PERIODS = {"day": "📅 День", "week": "🗓 Неделя", "all": "♾ Всё время"}


def _since_for(period: str, now) -> datetime | None:
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
    return None


async def _top_screen_text(session: AsyncSession, period: str) -> tuple[str, int | None]:
    """Возвращает (текст топа, место текущего юзера в 💬-топе или None)."""
    now = datetime.now(timezone.utc)
    since = _since_for(period, now)
    lines = [f"🏅 <b>Топы чата · {PERIODS[period]}</b>\n"]

    talkers = await top_messages(session, since, 10)
    my_rank = None
    lines.append("💬 <b>Болтуны</b>")
    if not talkers:
        lines.append("   пока пусто — будь первым! 💬")
    for i, (u, c) in enumerate(talkers, start=1):
        m = _medal(i)
        me = " 👈 <i>это ты</i>" if u.tg_id == _TOP_CTX.get("me") else ""
        if u.tg_id == _TOP_CTX.get("me"):
            my_rank = i
        lines.append(f"{m} {u.first_name} — {c} сообщ. (ур. {u.level}){me}")

    reactors = await top_reactions(session, since, 5)
    if reactors:
        lines.append("\n💖 <b>По полученным реакциям</b>")
        for i, (u, c) in enumerate(reactors, start=1):
            lines.append(f"{_medal(i)} {u.first_name} — {c}")

    streaks = await top_streaks(session, 5)
    if streaks:
        lines.append("\n🔥 <b>Серии дней</b>")
        for i, u in enumerate(streaks, start=1):
            lines.append(f"{_medal(i)} {u.first_name} — {u.streak_days} дн.")

    pets = await top_pets(session, 5)
    if pets:
        lines.append("\n🐾 <b>Питомцы</b>")
        for i, (p, owner) in enumerate(pets, start=1):
            lines.append(f"{_medal(i)} {p.name} (ур. {p.level}) · {owner}")

    levels = await top_levels(session, 5)
    if levels:
        lines.append("\n🏅 <b>Уровни игроков</b>")
        for i, u in enumerate(levels, start=1):
            lines.append(f"{_medal(i)} {u.first_name} — ур. {u.level}")

    lines.append("\n<i>/award — итоги прошлой недели с призами 🎁</i>")
    return "\n".join(lines), my_rank


# контекст «кто смотрит топ» (для подсветки своей строки)
_TOP_CTX: dict[str, int] = {}


@router.callback_query(F.data == "menu:top")
@router.callback_query(F.data.startswith("top:"))
async def top_screen(cb: CallbackQuery, session: AsyncSession) -> None:
    period = cb.data.split(":")[1] if ":" in cb.data and cb.data != "menu:top" else "week"
    if period not in PERIODS:
        period = "week"
    _TOP_CTX["me"] = cb.from_user.id
    text, _rank = await _top_screen_text(session, period)
    await cb.message.edit_text(text, reply_markup=top_tabs(period))
    await cb.answer()


@router.message(Command("top"))
async def cmd_top(message: Message, session: AsyncSession) -> None:
    """Алиас команды — показывает недельный топ прямо в ЛС."""
    _TOP_CTX["me"] = message.from_user.id
    text, _ = await _top_screen_text(session, "week")
    await message.answer(text, reply_markup=top_tabs("week"))


@router.callback_query(F.data == "menu:settings")
async def settings_stub(cb: CallbackQuery) -> None:
    await cb.message.edit_text(
        "⚙️ Настройки уведомлений появятся на Этапе 6:\n"
        "• 🔔 напоминания о питомце\n• 🌅 утренний/вечерний дайджест\n"
        "• 🎄 праздничные события\n\n"
        "Пока что я всегда на связи 😉",
        reply_markup=back_to_main(),
    )
    await cb.answer()


# ---------- командный алиас статистики ----------
@router.message(Command("stats"))
async def cmd_stats(message: Message, session: AsyncSession) -> None:
    await message.answer("👇 Воспользуйся меню:", reply_markup=main_menu())
