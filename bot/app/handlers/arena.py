"""Недельная Арена питомцев (PVP) + гардероб (кастомизация окраса/аксессуаров).

UX: экраны редактируют одно сообщение; бой даёт короткий отчёт поверх топа.
Команда /arena — текстовый алиас кнопки «🏟 Арена».
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.db.repositories import PetRepository, UserRepository
from app.i18n import t
from app.keyboards.inline import back_to_main, pet_hub, style_keyboard
from app.handlers.tamagotchi import set_pet_page
from app.services.pet_duels import arena_screen, fight
from app.services.tamagotchi import TamagotchiService
from app.utils.safe_edit import safe_edit_or_answer

router = Router(name="arena")


async def _pet_or_alert(cb: CallbackQuery, session: AsyncSession):
    pet = await PetRepository(session).get_by_user(cb.from_user.id)
    if pet is None:
        await cb.answer("🥚 Сначала заведи питомца: /start", show_alert=True)
    return pet


@router.callback_query(F.data == "arena:open")
async def arena_open(cb: CallbackQuery, session: AsyncSession) -> None:
    set_pet_page(cb.message.chat.id, 2)  # арена — страница «Досуг»
    text, kb = await arena_screen(session, cb.from_user.id)
    await safe_edit_or_answer(cb.message, text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "arena:noop")
async def arena_noop(cb: CallbackQuery) -> None:
    """Кнопка-заглушка, когда бой недоступен (кулдаун/лимит): просто подсказка."""
    await cb.answer("Питомец пока не готов к бою ⏳", show_alert=True)


@router.message(Command("arena"))
async def cmd_arena(message: Message, session: AsyncSession) -> None:
    text, kb = await arena_screen(session, message.from_user.id)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "arena:fight")
async def arena_fight(cb: CallbackQuery, session: AsyncSession) -> None:
    pet = await _pet_or_alert(cb, session)
    if pet is None:
        return
    result = await fight(session, pet)
    try:
        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "limit":
                msg = "😤 Лимит боёв на сегодня исчерпан — приходи завтра!"
            elif reason == "cooldown":
                msg = f"⏳ Питомец отдыхает. Следующий бой через {result.get('sec', 0)} сек."
            else:
                msg = "🔍 Равных соперников не нашлось — попробуй позже."
            await cb.answer(msg, show_alert=True)
        else:
            opp = result["opponent"]
            head = ("🏆 Победа! " if result["i_won"] else "🛡 Поражение — реванш рядом! ")
            report = (f"{head}{pet.name} ({result['power_a']}) vs "
                      f"{opp.name} ({result['power_b']}) · "
                      f"очков недели: {result['my_score']} · осталось боёв: {result['left']}")
            text, kb = await arena_screen(session, cb.from_user.id)
            await safe_edit_or_answer(cb.message, f"{report}\n\n{text}", reply_markup=kb)
            await cb.answer("🥊 Бой сыгран!")
    finally:
        await session.commit()  # баллы/XP боя не должны потеряться при сетевой ошибке


# ---------------------------------------------------------------------------
# 🎨 Стиль: окрасы и аксессуары (косметика за монеты)
# ---------------------------------------------------------------------------

def _style_text(pet, user, notice: str = "") -> str:
    svc = TamagotchiService(None)  # только словари каталога, БД не нужна
    color_key, worn = svc.customization(pet)
    color_name = svc.PET_COLORS[color_key][0] if color_key else "Классический"
    lines = [
        f"🎨 <b>Гардероб · {pet.name}</b>",
        f"Окрас: {color_name} · Аксессуары: {' '.join(worn) or '—'}",
        f"Баланс владельца: 🪙 {user.coins}",
    ]
    if notice:
        lines.insert(0, notice)
    return "\n".join(lines)


async def _style_screen(cb: CallbackQuery, session: AsyncSession,
                        pet, notice: str = "") -> None:
    users = UserRepository(session)
    user = await users.get(cb.from_user.id)
    svc = TamagotchiService(session)
    color_key, worn = svc.customization(pet)
    kb = style_keyboard(svc.PET_COLORS, svc.PET_ACCESSORIES, color_key, worn)
    await safe_edit_or_answer(cb.message, _style_text(pet, user, notice),
                              reply_markup=kb)


@router.callback_query(F.data == "pet:style")
async def style_open(cb: CallbackQuery, session: AsyncSession) -> None:
    set_pet_page(cb.message.chat.id, 1)  # гардероб — страница «Вещи»
    pet = await _pet_or_alert(cb, session)
    if pet is None:
        return
    await _style_screen(cb, session, pet)
    await cb.answer()


@router.callback_query(F.data.startswith("style:color:"))
async def style_color(cb: CallbackQuery, session: AsyncSession) -> None:
    pet = await _pet_or_alert(cb, session)
    if pet is None:
        return
    users = UserRepository(session)
    user = await users.get(cb.from_user.id)
    if user is None:
        await cb.answer("Сначала /start", show_alert=True)
        return
    key = cb.data.split(":", 2)[2]
    notice = await TamagotchiService(session).buy_color(session, pet, user, key)
    await _style_screen(cb, session, pet, notice)
    await cb.answer(notice[:120])


@router.callback_query(F.data.startswith("style:acc:"))
async def style_acc(cb: CallbackQuery, session: AsyncSession) -> None:
    pet = await _pet_or_alert(cb, session)
    if pet is None:
        return
    users = UserRepository(session)
    user = await users.get(cb.from_user.id)
    if user is None:
        await cb.answer("Сначала /start", show_alert=True)
        return
    emoji = cb.data.split(":", 2)[2]
    notice = await TamagotchiService(session).buy_accessory(session, pet, user, emoji)
    await _style_screen(cb, session, pet, notice)
    await cb.answer(notice[:120])
