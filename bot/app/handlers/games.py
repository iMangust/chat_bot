"""Мини-игры с питомцем (Этап 3.5): угадайка, РКШ, реакция.

UX: каждая игра — редактирование одного сообщения. Состояние игры хранится
в FSM, а не в callback_data, чтобы нельзя было «подсмотреть» секрет через
пересылку кнопок.

Баланс: победа = svc.play(pet, won=True) → XP/счастье; характеристики влияют
на честные условия игры (интеллект — диапазон подсказки, ловкость — бюджет
реакции). Победы идут в счётчик games_won для ачивки «Игумен».
"""
from __future__ import annotations

import random
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import PetRepository, UserRepository
from app.keyboards.inline import (
    guess_hint_keyboard, games_menu, pet_hub, reaction_keyboard, rps_keyboard,
)
from app.services.achievements import AchievementService
from app.utils.safe_edit import safe_edit_or_answer
from app.handlers.tamagotchi import set_pet_page
from app.services.tamagotchi import SPECIES_DATA, TamagotchiService, _species_key

router = Router(name="games")

RPS_EMOJI = {"rock": "🪨", "scissors": "✂️", "paper": "📄"}


class Games(StatesGroup):
    guessing = State()
    rps = State()
    reaction = State()


async def _get_pet(session: AsyncSession, tg_id: int):
    return await PetRepository(session).get_by_user(tg_id)


@router.callback_query(F.data == "pet:play")   # обратная совместимость со старой кнопкой
@router.callback_query(F.data == "pet:games")
async def games_screen(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await state.clear()
        await safe_edit_or_answer(cb.message, "🥚 Сначала заведи питомца (/start).",
                                  reply_markup=pet_hub(2))
        await cb.answer()
        return
    set_pet_page(cb.message.chat.id, 2)  # игры — страница «Досуг»
    sp = SPECIES_DATA.get(_species_key(pet), SPECIES_DATA["cat"])
    await state.clear()
    await safe_edit_or_answer(cb.message, 
        f"🎮 <b>Игровая с {pet.name}</b> {sp['emoji']}\n\n"
        "• 🔢 <i>Угадай число</i> — 🧠 интеллект сужает подсказку\n"
        "• ✂️ <i>Камень-ножницы-бумага</i> — честный рандом\n"
        "• ⚡ <i>Реакция</i> — жми «ЛОВИ!» быстрее; 🏃 ловкость даёт доп. время\n\n"
        "Победа: +15 XP и море счастья. Поражение всё равно даёт опыт!",
        reply_markup=games_menu(),
    )
    await cb.answer()


# ---------------------------------------------------------------------------
# Игра 1: угадай число (секрет живёт только в FSM)
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "game:guess")
async def start_guess(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    svc = TamagotchiService(session)
    secret, (lo, hi) = svc.guess_range(pet)
    await state.set_state(Games.guessing)
    await state.update_data(secret=secret, lo=lo, hi=hi)
    await safe_edit_or_answer(cb.message, 
        f"🔢 Питомец загадал число от 1 до 20. Друзья шепчут, что оно в диапазоне "
        f"<b>{lo}…{hi}</b> (чем умнее питомец, тем точнее подсказка!).\n\n"
        "Нажми кнопку-вариант или напиши своё число сообщением:",
        reply_markup=guess_hint_keyboard(lo, hi),
    )
    await cb.answer()


@router.callback_query(Games.guessing, F.data.startswith("guess:"))
async def do_guess_cb(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    secret = int(data.get("secret", -1))
    guess = int(cb.data.split(":")[1])
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await state.clear()
        return await cb.answer()
    svc = TamagotchiService(session)
    won = guess == secret
    result = await svc.play(pet, won)
    await state.clear()
    await PetRepository(session).log_action(pet.id, "game", value=int(won),
                                            meta={"kind": "guess", "guess": guess})
    if won:
        await bump_games_won(session, cb.from_user.id)
    hint = "" if won else f" Это было число <b>{secret}</b>."
    await safe_edit_or_answer(cb.message, f"{result}{hint}\n\n" + svc.render(pet),
                               reply_markup=games_menu())
    await cb.answer()


@router.message(Games.guessing, F.text & F.text.strip().isdigit())
async def do_guess_msg(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    secret = int(data.get("secret", -1))
    try:
        guess = int(message.text.strip())
    except ValueError:
        return
    pet = await _get_pet(session, message.from_user.id)
    if pet is None:
        await state.clear()
        return
    svc = TamagotchiService(session)
    won = guess == secret
    result = await svc.play(pet, won)
    await state.clear()
    await PetRepository(session).log_action(pet.id, "game", value=int(won),
                                            meta={"kind": "guess", "guess": guess})
    if won:
        await bump_games_won(session, message.from_user.id)
    hint = "" if won else f" Это было число <b>{secret}</b>."
    await message.answer(f"{result}{hint}", reply_markup=games_menu(),
                           parse_mode="HTML")


# ---------------------------------------------------------------------------
# Игра 2: камень-ножницы-бумага
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "game:rps")
async def start_rps(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    await state.set_state(Games.rps)
    await safe_edit_or_answer(cb.message, 
        "✂️ <b>Камень-ножницы-бумага!</b>\n\n"
        f"{pet.name} уже выбрал ход (честный рандом). Выбирай свой — откроемся одновременно.",
        reply_markup=rps_keyboard(),
    )
    await cb.answer()


@router.callback_query(Games.rps, F.data.startswith("rps:"))
async def play_rps(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    mine = cb.data.split(":")[1]
    if mine not in RPS_EMOJI:
        return await cb.answer()
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await state.clear()
        return await cb.answer()
    theirs = random.choice(list(RPS_EMOJI))
    won = TamagotchiService.rps_beats(mine) == theirs
    draw = theirs == mine
    svc = TamagotchiService(session)
    result = await svc.play(pet, won)
    await state.clear()
    await PetRepository(session).log_action(pet.id, "game", value=int(won),
                                            meta={"kind": "rps", "mine": mine,
                                                  "theirs": theirs})
    if won:
        await bump_games_won(session, cb.from_user.id)
    outcome = "🤝 Ничья!" if draw else ("🎉 Ты выиграл!" if won else "😿 Питомец хитрее…")
    await safe_edit_or_answer(cb.message, 
        f"Ты: {RPS_EMOJI[mine]} · {pet.name}: {RPS_EMOJI[theirs]} — {outcome}\n\n"
        f"{result}\n\n" + svc.render(pet),
        reply_markup=games_menu(),
    )
    await cb.answer()


# ---------------------------------------------------------------------------
# Игра 3: реакция. Античит: время старта подписано в callback_data + FSM.
# ---------------------------------------------------------------------------
REACTION_GRACE_MS = 400  # допуск на сетевую задержку «опережающего» клика


@router.callback_query(F.data == "game:reaction")
async def start_reaction(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    budget = TamagotchiService(session).reaction_ms_budget(pet)
    ts = datetime.now(timezone.utc).isoformat()
    await state.set_state(Games.reaction)
    await state.update_data(react_start=ts, budget=budget)
    await safe_edit_or_answer(cb.message, 
        f"⚡ <b>Тест реакции!</b>\n\n"
        f"Нажми «ЛОВИ!» быстрее, чем за <b>{budget} мс</b>.\n"
        f"🏃 Ловкость питомца = +60 мс за каждый пункт. Пошёл!",
        reply_markup=reaction_keyboard(ts),
    )
    await cb.answer()


@router.callback_query(Games.reaction, F.data.startswith("react:"))
async def finish_reaction(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    started_iso = data.get("react_start")
    if not started_iso or not cb.data.endswith(started_iso):
        await state.clear()
        return await cb.answer("Ход уже сделан — начни игру заново", show_alert=True)
    try:
        started = datetime.fromisoformat(started_iso)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
    except ValueError:
        await state.clear()
        return await cb.answer("Игра сломалась, начни заново", show_alert=True)
    elapsed_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000.0
    budget = int(data.get("budget", 1500))
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await state.clear()
        return await cb.answer()
    svc = TamagotchiService(session)
    won = elapsed_ms <= budget + REACTION_GRACE_MS
    result = await svc.play(pet, won)
    await state.clear()
    await PetRepository(session).log_action(pet.id, "game", value=int(won),
                                            meta={"kind": "reaction", "ms": int(elapsed_ms)})
    if won:
        await bump_games_won(session, cb.from_user.id)
    await safe_edit_or_answer(cb.message, 
        f"⏱ Твоё время: <b>{int(elapsed_ms)} мс</b> (бюджет {budget} мс)\n\n"
        f"{result}\n\n" + svc.render(pet),
        reply_markup=games_menu(),
    )
    await cb.answer()


# ---------------------------------------------------------------------------
async def bump_games_won(session: AsyncSession, tg_id: int) -> None:
    """Счётчик побед для ачивки games_won_10 («Игумен»)."""
    users = UserRepository(session)
    new_val = await users.bump_stat(tg_id, "games_won", 1)
    await AchievementService(session).check(tg_id, {"games_won": new_val})
