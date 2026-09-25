"""Экраны «Друзья питомцев» и «🖼 Карточка профиля» (Этапы 5–6)."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pet
from app.db.repositories import AchievementRepository, PetRepository, UserRepository
from app.keyboards.inline import back_to_main, main_menu
from app.services.achievements import AchievementService
from app.services.pet_social import (MAX_FRIENDS, list_friends, make_friends,
                                     render_friend_list, suggest_friend)
from app.services.profile_card import get_or_render_card
from app.handlers.tamagotchi import set_pet_page
from app.services.tamagotchi import SPECIES_DATA
from app.utils.safe_edit import safe_edit_or_answer

router = Router(name="social")


def _friend_kb(pet_name: str, other_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🤝 Познакомиться с {pet_name}", callback_data=f"fr:add:{other_id}")
    kb.row()
    kb.button(text="🐾 К питомцу", callback_data="menu:pet")
    return kb.as_markup()


async def _friends_screen(cb: CallbackQuery, session: AsyncSession, note: str = "") -> None:
    set_pet_page(cb.message.chat.id, 2)  # друзья — страница «Досуг»
    pet = await PetRepository(session).get_by_user(cb.from_user.id)
    if pet is None:
        await cb.answer("Сначала заведи питомца!", show_alert=True)
        return
    friends = await list_friends(session, pet.id)
    text = render_friend_list(pet, friends)
    markup = back_to_main()
    if len(friends) < MAX_FRIENDS:
        sug = await suggest_friend(session, pet)
        if sug is not None:
            key = str(getattr(sug.species, "value", sug.species))
            sp_emoji = SPECIES_DATA.get(key, {}).get("emoji", "🐾")
            text += (f"\n\n💡 <b>Рекомендация:</b> {sp_emoji} <b>{html.escape(sug.name)}</b> "
                     f"(ур. {sug.level}) ждёт знакомства!")
            markup = _friend_kb(html.escape(sug.name), sug.id)
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
        # ачивка «Новые знакомства»: оба получают прогресс pet_walks-ачивки walk_friend
        ach = AchievementService(session)
        await ach.unlock_by_code(int(pet.user_id), "walk_friend")
        await ach.unlock_by_code(int(other.user_id), "walk_friend")
    await session.commit()
    logger.info("friend add {}+{}: {}", pet.id, other.id, ok)
    await _friends_screen(cb, session, note=("✅ " if ok else "ℹ️ ") + msg)
    await cb.answer(msg[:50])


# ─── 🖼 Карточка профиля (PNG через Pillow) ──────────────────────────────────
async def _send_card(message: Message, session: AsyncSession, tg_id: int) -> None:
    png, changed = await get_or_render_card(session, tg_id)
    if not png:
        await message.answer("Не удалось собрать карточку — сначала /start 🙂")
        return
    await message.answer_photo(
        BufferedInputFile(png, filename="profile.png"),
        caption="🪪 Твоя карточка игрока. Обновляется автоматически при росте статов.",
        reply_markup=back_to_main(),
    )


@router.callback_query(F.data == "menu:card")
async def cb_card(cb: CallbackQuery, session: AsyncSession) -> None:
    if cb.message is None:
        await cb.answer("Нет сообщения-контекста 😅 Нажми /start", show_alert=True)
        return
    # Если исходное сообщение без текста (фото/стикер/кружок) — edit_text
    # невозможен в принципе; навигацию обеспечивает кнопка «Назад» под фото.
    await _send_card(cb.message, session, cb.from_user.id)
    await cb.answer("Карточка готова ✨")


# Кнопка ⬅️ Назад под фото карточки ведёт на callback menu:main, который
# умеет отвечать и в пустых/медиа-сообщениях — см. start.cb_main_menu.
@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery) -> None:
    """Информационные кнопки (номер страницы и т.п.) — просто гасим часы."""
    await cb.answer()


@router.message(Command("card", "profile"))
async def cmd_card(message: Message, session: AsyncSession) -> None:
    await _send_card(message, session, message.from_user.id)
