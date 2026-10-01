from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.db.models import User
from app.db.repositories import NotificationRepository, UserRepository
from app import themes
from app.keyboards.inline import (back_to_main, settings_keyboard,
                                  theme_picker_keyboard)
from app.services.leaderboard import leaderboard_text, snapshot_weekly
from app.utils.safe_edit import safe_edit_or_answer, answer_safe

router = Router(name="settings")

_FLAG_LABELS = {
    "pet_reminders": "🐾 Питомец скучает",
    "streak_reminders": "🔥 Стрик под угрозой",
    "achievement_notifications": "🏆 Достижения",
    "daily_report": "🌅 Ежедневный отчёт",
}


def _theme_intro() -> str:
    theme = themes.THEMES.get(themes.current_theme_key())
    if theme and theme.settings_intro:
        return theme.settings_intro
    return ("⚙️ <b>Настройки уведомлений</b>\n\n"
            "Я пишу в ЛС только когда это действительно нужно.\n"
            "Здесь можно всё отключить — нажми на тумблер:\n\n")


async def _render_settings(session: AsyncSession, message: Message, tg_id: int,
                           chat_id: int | None = None) -> None:
    ns = await NotificationRepository(session).get_or_create(tg_id)
    flags = {k: bool(getattr(ns, k)) for k in _FLAG_LABELS}
    text = (_theme_intro()
            + "\n".join(f"{'✅' if flags[k] else '❌'} {label}"
                        for k, label in _FLAG_LABELS.items()))
    # Блок выбора темы оформления
    db_user = await session.get(User, tg_id)
    current_key = ((db_user.settings_extra or {}).get("theme")
                   if db_user else None) or themes.DEFAULT_THEME_KEY
    cur = themes.theme_for_key(current_key)
    text += ("\n\n🎭 <b>Тема оформления</b>\n"
             f"Сейчас: <b>{cur.title}</b> — {cur.tagline}\n"
             "Выбери другую:")
    kb = settings_keyboard(flags, chat_id)
    theme_rows = theme_picker_keyboard(cur.key).inline_keyboard
    kb.inline_keyboard = theme_rows + list(kb.inline_keyboard)
    await safe_edit_or_answer(message, text, reply_markup=kb)

@router.callback_query(F.data == "menu:settings")
async def cb_settings(cb: CallbackQuery, session: AsyncSession) -> None:
    await _render_settings(session, cb.message, cb.from_user.id,
                           chat_id=cb.message.chat.id if cb.message else None)
    await cb.answer()

@router.callback_query(F.data.startswith("set:theme:"))
async def cb_set_theme(cb: CallbackQuery, session: AsyncSession) -> None:
    key = cb.data.split(":", 2)[2]
    if key not in themes.THEMES:
        await cb.answer("Неизвестная тема", show_alert=True)
        return
    user = await UserRepository(session).get_or_create(
        cb.from_user.id, cb.from_user.first_name or "", cb.from_user.username)
    extra = dict(user.settings_extra or {})
    extra["theme"] = key
    user.settings_extra = extra
    flag_modified(user, "settings_extra")
    await session.commit()
    themes.set_theme(key)  # сразу перекрашиваем ответ и последующие экраны
    th = themes.theme_for_key(key)
    await _render_settings(session, cb.message, cb.from_user.id,
                           chat_id=cb.message.chat.id if cb.message else None)
    await cb.answer(f"Тема изменена: {th.title}")


@router.callback_query(F.data.startswith("set:"))
async def cb_toggle(cb: CallbackQuery, session: AsyncSession) -> None:
    key = cb.data.split(":", 1)[1]
    if key not in _FLAG_LABELS:
        await cb.answer("Неизвестная настройка", show_alert=True)
        return
    repo = NotificationRepository(session)
    ns = await repo.get_or_create(cb.from_user.id)
    new = not bool(getattr(ns, key))
    setattr(ns, key, new)
    await session.commit()
    await _render_settings(session, cb.message, cb.from_user.id,
                           chat_id=cb.message.chat.id if cb.message else None)
    await cb.answer(("Включено: " if new else "Выключено: ") + _FLAG_LABELS[key])

@router.message(Command("award", "awards"), F.chat.type == "private")
async def cmd_awards(message: Message, session: AsyncSession) -> None:
    payload = await snapshot_weekly(session)
    if not payload:
        await message.answer("Пока нечего показать — топ будет после первой недели 🏁")
        return
    await answer_safe(message, leaderboard_text(payload),
                      reply_markup=back_to_main())

@router.message(Command("settings"), F.chat.type == "private")
async def cmd_settings(message: Message, session: AsyncSession) -> None:
    await _render_settings(session, message, message.from_user.id)
