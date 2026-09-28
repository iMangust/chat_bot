"""⚙️ Экран настроек уведомлений + командные алиасы топов/карточки."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import NotificationRepository
from app.keyboards.inline import back_to_main, main_menu, settings_keyboard
from app.services.leaderboard import leaderboard_text, snapshot_weekly
from app.utils.safe_edit import safe_edit_or_answer, answer_safe

router = Router(name="settings")

_FLAG_LABELS = {
    "pet_reminders": "🐾 Питомец скучает",
    "streak_reminders": "🔥 Стрик под угрозой",
    "achievement_notifications": "🏆 Достижения",
    "daily_report": "🌅 Ежедневный отчёт",
}

async def _render_settings(session: AsyncSession, message: Message, tg_id: int) -> None:
    ns = await NotificationRepository(session).get_or_create(tg_id)
    flags = {k: bool(getattr(ns, k)) for k in _FLAG_LABELS}
    text = ("⚙️ <b>Настройки уведомлений</b>\n\n"
            "Я пишу в ЛС только когда это действительно нужно.\n"
            "Здесь можно всё отключить — нажми на тумблер:\n\n"
            + "\n".join(f"{'✅' if flags[k] else '❌'} {label}" for k, label in _FLAG_LABELS.items()))
    await safe_edit_or_answer(message, text, reply_markup=settings_keyboard(flags))

@router.callback_query(F.data == "menu:settings")
async def cb_settings(cb: CallbackQuery, session: AsyncSession) -> None:
    await _render_settings(session, cb.message, cb.from_user.id)
    await cb.answer()

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
    await _render_settings(session, cb.message, cb.from_user.id)
    await cb.answer(("Включено: " if new else "Выключено: ") + _FLAG_LABELS[key])

@router.message(Command("award", "awards"), F.chat.type == "private")
async def cmd_awards(message: Message, session: AsyncSession) -> None:
    """Итоги прошлой недели + выданные призы."""
    payload = await snapshot_weekly(session)
    if not payload:
        await message.answer("Пока нечего показать — топ будет после первой недели 🏁")
        return
    await answer_safe(message, leaderboard_text(payload),
                      reply_markup=back_to_main())

@router.message(Command("settings"), F.chat.type == "private")
async def cmd_settings(message: Message, session: AsyncSession) -> None:
    """Полноценный экран настроек прямо в ЛС (раньше — заглушка «Воспользуйся меню»)."""
    await _render_settings(session, message, message.from_user.id)
