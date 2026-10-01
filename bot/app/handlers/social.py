from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pet
from app.db.repositories import PetRepository
from app.keyboards.inline import (back_to_main, nav_row, with_nav)
from app.services.achievements import AchievementService
from app.services.pet_social import (MAX_FRIENDS, list_friends, make_friends,
                                     render_friend_list, suggest_friend)
from app.services.profile_card import get_or_render_card
from app.handlers.tamagotchi import set_pet_page
from app.services.tamagotchi import SPECIES_DATA
from app.utils.safe_edit import safe_edit_or_answer







def _vrow(b):
    b._markup = [list([btn]) for btn in list(b.buttons)]
    b.max_width = 1


router = Router(name="social")

def _friend_kb(pet_name: str, other_id: int,
               chat_id: int | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🤝 Познакомиться с {pet_name}", callback_data=f"fr:add:{other_id}")
    _vrow(kb)
    # «⬅️ Назад» — по истории (обычно вкладка «🎮 Досуг»), «🏠 Меню» — домой.
    with_nav(kb, "friends", chat_id)
    return kb.as_markup()

async def _friends_screen(cb: CallbackQuery, session: AsyncSession, note: str = "") -> None:
    set_pet_page(cb.message.chat.id, 2)
    pet = await PetRepository(session).get_by_user(cb.from_user.id)
    if pet is None:
        await cb.answer("Сначала заведи питомца!", show_alert=True)
        return
    friends = await list_friends(session, pet.id)
    text = render_friend_list(pet, friends)
    chat_id = cb.message.chat.id if cb.message else None
    b = InlineKeyboardBuilder()
    b.button(text="🐾 К питомцу", callback_data="menu:pet")
    with_nav(b, "friends", chat_id)
    markup = b.as_markup()
    if len(friends) < MAX_FRIENDS:
        sug = await suggest_friend(session, pet)
        if sug is not None:
            key = str(getattr(sug.species, "value", sug.species))
            sp_emoji = SPECIES_DATA.get(key, {}).get("emoji", "🐾")
            text += (f"\n\n💡 <b>Рекомендация:</b> {sp_emoji} <b>{html.escape(sug.name)}</b> "
                     f"(ур. {sug.level}) ждёт знакомства!")
            markup = _friend_kb(html.escape(sug.name), sug.id, chat_id)
    if note:
        text = f"{note}\n\n{text}"
    await safe_edit_or_answer(cb.message, text, reply_markup=markup)

@router.callback_query(F.data == "pet:friends")
async def cb_friends(cb: CallbackQuery, session: AsyncSession) -> None:
    await _friends_screen(cb, session)
    await cb.answer()

@router.callback_query(F.data.startswith("fr:add:"))
async def cb_friend_add(cb: CallbackQuery, session: AsyncSession) -> None:
    pet = await PetRepository(session).get_by_user(cb.from_user.id)
    try:
        other_id = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer("Битая кнопка 😅", show_alert=True)
        return
    other = await session.get(Pet, other_id)
    if pet is None or other is None:
        await cb.answer("Питомец не найден", show_alert=True)
        return
    ok, msg = await make_friends(session, pet, other)
    if ok and pet.user_id != other.user_id:
        ach = AchievementService(session)
        await ach.unlock_by_code(int(pet.user_id), "walk_friend")
        await ach.unlock_by_code(int(other.user_id), "walk_friend")
    await session.commit()
    logger.info("friend add {}+{}: {}", pet.id, other.id, ok)
    await _friends_screen(cb, session, note=("✅ " if ok else "ℹ️ ") + msg)
    await cb.answer(msg[:50])

def _card_kb(chat_id: int | None = None) -> InlineKeyboardMarkup:
    """Карточка профиля — верхнеуровневый экран без подуровней: только
    «🏠 Меню» (кнопка «Назад» дублировала бы её или вела в себя)."""
    b = InlineKeyboardBuilder()
    b.row(*nav_row("card", back_cb=""))
    return b.as_markup()


async def _send_card(message: Message, session: AsyncSession, tg_id: int,
                     chat_id: int | None = None) -> None:
    png, changed = await get_or_render_card(session, tg_id)
    if not png:
        await message.answer("Не удалось собрать карточку — сначала /start 🙂")
        return
    pet = await PetRepository(session).get_by_user(tg_id)
    hint = "" if pet is not None else " Питомец появится на карточке, когда ты его заведёшь."
    await message.answer_photo(
        BufferedInputFile(png, filename="profile.png"),
        caption=("🪪 Твоя карточка игрока: ранг, питомец во всех деталях, арена и динамика."
                 + hint + " Обновляется автоматически при росте статов."),
        reply_markup=_card_kb(chat_id),
    )

@router.callback_query(F.data == "menu:card")
async def cb_card(cb: CallbackQuery, session: AsyncSession) -> None:
    if cb.message is None:
        await cb.answer("Нет сообщения-контекста 😅 Нажми /start", show_alert=True)
        return
    await _send_card(cb.message, session, cb.from_user.id,
                     chat_id=cb.message.chat.id if cb.message else None)
    await cb.answer("Карточка готова ✨")

@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery) -> None:
    await cb.answer()

@router.message(Command("card", "profile"), F.chat.type == "private")
async def cmd_card(message: Message, session: AsyncSession) -> None:
    await _send_card(message, session, message.from_user.id,
                     chat_id=message.chat.id)
