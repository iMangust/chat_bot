"""Хендлеры /start, онбординг (имя питомца), главное меню."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pet, PetSpecies
from app.db.repositories import PetRepository, UserRepository
from app.keyboards.inline import (
    main_menu, onboard_done, species_picker, start_pet_name_suggestions,
    welcome_start_button,
)
from app.services.achievements import AchievementService
from app.services.tamagotchi import SPECIES_DATA
from app.utils.formatting import progress_bar, xp_needed_for_level

router = Router(name="start")

PET_NAME_SUGGESTIONS = ["Барсик", "Мурка", "Персик", "Кузя", "Соня", "Имя своё…"]


class Onboarding(StatesGroup):
    choosing_pet_species = State()
    choosing_pet_name = State()


def species_picker_text() -> str:
    """Описание видов для экрана выбора (используется в онбординге)."""
    lines = []
    for code, sp in SPECIES_DATA.items():
        st = sp["start"]
        likes = ", ".join(k for k, v in sp["prefers"].items() if v > 0) or "—"
        dislikes = ", ".join(k for k, v in sp["prefers"].items() if v < 0) or "нет"
        lines.append(
            f"{sp['emoji']} <b>{sp['title']}</b> — {sp['desc']}\n"
            f"   💪{st['strength']} 🏃{st['agility']} 🧠{st['intellect']} · любит: {likes} · не любит: {dislikes}"
        )
    return "\n".join(lines)


WELCOME_DM = (
    "👋 Привет, <b>{name}</b>!\n\n"
    "Я — бот-компаньон нашего чата. Здесь я:\n"
    "• 🐾 слежу за твоей активностью и выдаю достижения;\n"
    "• 🥚 дарю виртуального питомца, о котором можно заботиться;\n"
    "• 🏅 показываю топы болтунов и реакций.\n\n"
    "Правила простые: общайся в чате, не флуди, будь добрым. "
    "За обычные сообщения капает XP и монеты.\n\n"
    "Нажми «Начать», чтобы завести питомца!"
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, session: AsyncSession,
                    command: CommandObject | None = None) -> None:
    """Основной вход: регистрация + приглашение к онбордингу.

    deep-link `tamabot?start=invite_<tg_id>` учтём на этапе 6 (ачивка «Знакомый»).
    """
    users = UserRepository(session)
    user = await users.get_or_create(
        tg_id=message.from_user.id,
        first_name=message.from_user.first_name or "",
        username=message.from_user.username,
    )
    if not user.onboarded:
        await message.answer(
            WELCOME_DM.format(name=user.first_name or "друг"),
            reply_markup=welcome_start_button(),
        )
        return
    await message.answer("🏠 Главное меню:", reply_markup=main_menu())


@router.callback_query(F.data == "onb:start")
async def cb_onboard_start(cb: CallbackQuery, state: FSMContext,
                           session: AsyncSession) -> None:
    users = UserRepository(session)
    user = await users.get_or_create(cb.from_user.id, cb.from_user.first_name or "",
                                     cb.from_user.username)
    if user.onboarded:
        await cb.message.edit_text("Ты уже с нами! 🎉", reply_markup=main_menu())
        await cb.answer()
        return
    await state.set_state(Onboarding.choosing_pet_species)
    await cb.message.edit_text(
        "🐣 Шаг 1 из 3. Выбери питомца — у каждого свой характер и бонусы:\n\n"
        + species_picker_text(),
        reply_markup=species_picker(),
    )
    await cb.answer()


@router.callback_query(Onboarding.choosing_pet_species, F.data.startswith("onb:species:"))
async def cb_pick_species(cb: CallbackQuery, state: FSMContext) -> None:
    code = cb.data.split(":")[2]
    if code not in SPECIES_DATA:
        await cb.answer("Такого питомца нет", show_alert=True)
        return
    await state.update_data(species=code)
    await state.set_state(Onboarding.choosing_pet_name)
    await cb.message.edit_text(
        f"{SPECIES_DATA[code]['emoji']} Отличный выбор — {SPECIES_DATA[code]['title']}!\n\n"
        "Шаг 2 из 3. Выбери имя питомцу (или напиши своё сообщением):",
        reply_markup=start_pet_name_suggestions(PET_NAME_SUGGESTIONS),
    )
    await cb.answer()


@router.callback_query(Onboarding.choosing_pet_name, F.data.startswith("onb:name:"))
async def cb_pick_name(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    name = cb.data.split(":", 2)[2]
    if name == "Имя своё…":
        await cb.message.edit_text("✍️ Напиши своё имя питомца сообщением:")
        return
    data = await state.get_data()
    species_code = data.get("species", "cat")
    await _finish_onboarding(cb, state, session, name, species=_to_species_enum(species_code))


@router.message(Onboarding.choosing_pet_name, F.text & ~F.text.startswith("/"))
async def msg_custom_name(message: Message, state: FSMContext,
                          session: AsyncSession) -> None:
    raw = (message.text or "").strip()
    name = raw[:32] if raw else "Мурка"
    data = await state.get_data()
    species_code = data.get("species", "cat")
    await _finish_onboarding_from_msg(message, state, session, name, species_code)


def _to_species_enum(code: str) -> PetSpecies:
    try:
        return PetSpecies(code)
    except ValueError:
        return PetSpecies.cat


async def _finish_onboarding_from_msg(message: Message, state: FSMContext,
                                      session: AsyncSession, name: str,
                                      species_code: str = "cat") -> None:
    users = UserRepository(session)
    user = await users.get_or_create(message.from_user.id, message.from_user.first_name or "",
                                     message.from_user.username)
    pets = PetRepository(session)
    pet = await pets.get_by_user(user.tg_id)
    if pet is None:
        pet = await pets.create(Pet(user_id=user.tg_id, name=name,
                                    species=_to_species_enum(species_code)))
    user.pet_name = name
    user.onboarded = True
    await state.clear()
    await pets.log_action(pet.id, "born")
    ach = AchievementService(session)
    await ach.unlock_by_code(user.tg_id, "first_steps")
    await message.answer(
        f"🎉 У тебя появился питомец <b>{name}</b> — {SPECIES_DATA[species_code]['emoji']} "
        f"{SPECIES_DATA[species_code]['title']}!\n\n"
        "Шаг 3 из 3 — мини-тур:\n"
        "• 🐾 Питомец — корми, мой, играй (статы падают со временем!)\n"
        "• 📊 Статы — твоя активность и уровень\n"
        "• 🏆 Достижения — собирай награды\n"
        "• 🏅 Топы — кто тут главный болтун\n\n"
        "Совет: зайди в группу и напиши что-нибудь — это засчитается как активность 👇",
        reply_markup=onboard_done(),
    )


async def _finish_onboarding(cb: CallbackQuery, state: FSMContext,
                             session: AsyncSession, name: str,
                             species: PetSpecies) -> None:
    users = UserRepository(session)
    user = await users.get_or_create(cb.from_user.id, cb.from_user.first_name or "",
                                     cb.from_user.username)
    pets = PetRepository(session)
    pet = await pets.get_by_user(user.tg_id)
    if pet is None:
        pet = await pets.create(Pet(user_id=user.tg_id, name=name, species=species))
    user.pet_name = name
    user.onboarded = True
    await state.clear()
    await pets.log_action(pet.id, "born")
    ach = AchievementService(session)
    await ach.unlock_by_code(user.tg_id, "first_steps")
    sp = SPECIES_DATA.get(species.value, SPECIES_DATA["cat"])
    await cb.message.edit_text(
        f"🎉 У тебя появился питомец <b>{name}</b> — {sp['emoji']} {sp['title']}!\n\n"
        "Мини-тур:\n"
        "• 🐾 Питомец — корми, мой, играй (статы падают со временем!)\n"
        "• 📊 Статы — твоя активность и уровень\n"
        "• 🏆 Достижения — собирай награды\n"
        "• 🏅 Топы — кто тут главный болтун\n\n"
        "Зайди в группу и напиши что-нибудь — это засчитается как активность 👇",
        reply_markup=onboard_done(),
    )
    await cb.answer()


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(cb: CallbackQuery, session: AsyncSession) -> None:
    users = UserRepository(session)
    user = await users.get_or_create(cb.from_user.id, cb.from_user.first_name or "",
                                     cb.from_user.username)
    need = xp_needed_for_level(user.level)
    bar = progress_bar(user.xp, need)
    await cb.message.edit_text(
        f"🏠 <b>Главное меню</b>\n\n"
        f"👤 {user.first_name}, уровень {user.level} · {bar} {user.xp}/{need} XP\n"
        f"🪙 Монеты: {user.coins} · 🔥 Серия: {user.streak_days} дн.",
        reply_markup=main_menu(),
    )
    await cb.answer()
