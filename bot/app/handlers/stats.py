"""Экраны статистики, достижений и топов."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import ActivityRepository, UserRepository
from app.keyboards.inline import achievements_list, back_to_main, main_menu
from app.services.activity import ActivityService
from app.services.achievements import AchievementService
from app.utils.formatting import progress_bar, xp_needed_for_level

router = Router(name="stats")


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


@router.callback_query(F.data == "menu:top")
async def top_screen(cb: CallbackQuery, session: AsyncSession) -> None:
    users = UserRepository(session)
    repo = ActivityRepository(session)
    talkers = await users.top_by("messages_count", 10)
    streaks = await users.top_by("streak_days", 5)

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏅 <b>Топы болтунов (за всё время)</b>\n"]
    for i, u in enumerate(talkers, start=1):
        m = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{m} {u.first_name} — {u.messages_count} сообщ. (ур. {u.level})")
    lines.append("\n🔥 <b>Серии дней</b>")
    for i, u in enumerate(streaks, start=1):
        m = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{m} {u.first_name} — {u.streak_days} дн.")
    await cb.message.edit_text("\n".join(lines), reply_markup=back_to_main())
    await cb.answer()


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


# ---------- командные алиасы (UX кнопочный, команды дублируют) ----------
@router.message(F.text == "/stats")
async def cmd_stats(message: Message, session: AsyncSession) -> None:
    await message.answer("👇 Воспользуйся меню:", reply_markup=main_menu())


@router.message(F.text == "/top")
async def cmd_top(message: Message, session: AsyncSession) -> None:
    await message.answer("👇 Воспользуйся меню:", reply_markup=main_menu())
