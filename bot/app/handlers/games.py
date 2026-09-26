"""Мини-игры с питомцем: угадайка, РКШ, «21» (блэкджек против питомца-дилера).

UX: каждая игра — редактирование одного сообщения. Состояние игры хранится
в FSM, а не в callback_data, чтобы нельзя было «подсмотреть» секрет через
пересылку кнопок.

Баланс: победа = svc.play(pet, won=True) → XP/счастье; характеристики влияют
на честные условия игры (интеллект — диапазон подсказки и «выдержка» дилера
в «21»). Победы идут в счётчик games_won для ачивки «Игумен».
"""
from __future__ import annotations

import random
from app.utils.local_time import now as local_now

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import PetRepository, UserRepository
from app.keyboards.inline import (
    guess_hint_keyboard, games_menu, pet_hub, rps_keyboard, twentyone_keyboard,
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
    blackjack = State()


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
        "• 🃏 <i>Двадцать одно</i> — набери ≤21; 🧠 интеллект делает дилера «мягче»\n\n"
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
    await safe_edit_or_answer(cb.message, f"{result}{hint}\n\n" + await svc.render_async(pet),
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
    won = TamagotchiService.rps_beaten_by(mine) == theirs
    draw = theirs == mine
    svc = TamagotchiService(session)
    result = await svc.play(pet, won)
    await state.clear()
    await PetRepository(session).log_action(pet.id, "game", value=int(won),
                                            meta={"kind": "rps", "mine": mine,
                                                  "theirs": theirs})
    if won:
        await bump_games_won(session, cb.from_user.id)
    outcome = "🤝 Ничья!" if draw else ("🎉 Ты выиграл! Питомец не угадал твой ход." if won else "😿 Питомец хитрее…")
    await safe_edit_or_answer(cb.message, 
        f"Ты: {RPS_EMOJI[mine]} · {pet.name}: {RPS_EMOJI[theirs]} — {outcome}\n\n"
        f"{result}\n\n" + await svc.render_async(pet),
        reply_markup=games_menu(),
    )
    await cb.answer()


# ---------------------------------------------------------------------------
# Игра 3: «21» (блэкджек). Питомец — дилер; античит: колода подписана в FSM.
# ---------------------------------------------------------------------------
BJ_DECK = [(r, s) for r in range(2, 11) for s in ("♠", "♥", "♦", "♣")]


def _bj_value(cards: list[tuple[int, str]]) -> int:
    """Очки руки: туз = 11, пока не перебор; иначе 1."""
    total = 0
    aces = 0
    for rank, _suit in cards:
        if rank == 11:
            aces += 1
            total += 11
        else:
            total += max(2, min(rank, 10))
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def _bj_render(cards: list[tuple[int, str]], hidden: bool = False) -> str:
    if hidden and cards:
        return f"{_card_str(cards[0])} + 🂠"
    return _card_str(cards)


def _card_str(cards: list[tuple[int, str]]) -> str:
    labels = {2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9", 10: "10", 11: "Т"}
    return " ".join(f"{labels[r]}{s}" for r, s in cards) or "—"


@router.callback_query(F.data == "game:blackjack")
async def start_blackjack(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    svc = TamagotchiService(session)
    rng = random.Random()
    deck = BJ_DECK[:]
    rng.shuffle(deck)
    # 🧠 интеллект питомца = «хитрость» дилера: умный чаще пасует на 17, глупый тянет до 18+
    dealer_stay = 17 + min(3, pet.intellect // 6)
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    await state.set_state(Games.blackjack)
    await state.update_data(deck=deck, player=player, dealer=dealer, stay=dealer_stay)
    await safe_edit_or_answer(cb.message,
        f"🃏 <b>Двадцать одно!</b> {pet.name} — дилер.\n\n"
        f"Твои карты: <b>{_bj_render(player)}</b> ({_bj_value(player)})\n"
        f"Карты дилера: <b>{_bj_render(dealer, hidden=True)}</b>\n\n"
        "«Ещё» — взять карту, «Хватит» — остановиться. Больше 21 — перебор!",
        reply_markup=twentyone_keyboard(),
    )
    await cb.answer()


async def _bj_finish(cb: CallbackQuery, state: FSMContext, session: AsyncSession,
                     player: list, dealer: list) -> None:
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await state.clear()
        return await cb.answer()
    svc = TamagotchiService(session)
    pv, dv = _bj_value(player), _bj_value(dealer)
    if pv > 21:
        outcome, won = "💥 Перебор! Питомец забирает сдачу.", False
    elif dv > 21:
        outcome, won = f"🎉 Дилер перебрал ({dv}) — ты забрал банк!", True
    elif pv > dv:
        outcome, won = f"🎉 Ты выиграл: {pv} против {dv}!", True
    elif pv == dv:
        outcome, won = f"🤝 Ничья: по {pv}.", False
    else:
        outcome, won = f"😿 Питомец-дилер хитрее: {dv} против {pv}.", False
    result = await svc.play(pet, won)
    await state.clear()
    await PetRepository(session).log_action(pet.id, "game", value=int(won),
                                            meta={"kind": "blackjack", "player": pv, "dealer": dv})
    if won:
        await bump_games_won(session, cb.from_user.id)
    await safe_edit_or_answer(cb.message,
        f"Твои: <b>{_bj_render(player)}</b> ({pv}) · {pet.name}: <b>{_bj_render(dealer)}</b> ({dv})\n"
        f"{outcome}\n\n{result}",
        reply_markup=games_menu(),
    )
    await cb.answer()


@router.callback_query(Games.blackjack, F.data == "bj:hit")
async def bj_hit(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    deck, player = list(data.get("deck") or []), list(data.get("player") or [])
    dealer = list(data.get("dealer") or [])
    if not deck:
        await state.clear()
        return await cb.answer("Колода кончилась — начни игру заново", show_alert=True)
    player.append(deck.pop())
    pv = _bj_value(player)
    if pv >= 21:
        return await _bj_finish(cb, state, session, player, dealer)
    await state.update_data(deck=deck, player=player)
    await safe_edit_or_answer(cb.message,
        f"🃏 Твои карты: <b>{_bj_render(player)}</b> ({pv})\n"
        f"Карты дилера: <b>{_bj_render(dealer, hidden=True)}</b>\n\n"
        "Ещё или хватит?",
        reply_markup=twentyone_keyboard(),
    )
    await cb.answer()


@router.callback_query(Games.blackjack, F.data == "bj:stand")
async def bj_stand(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    deck, player = list(data.get("deck") or []), list(data.get("player") or [])
    dealer = list(data.get("dealer") or [])
    stay = int(data.get("stay", 17))
    while _bj_value(dealer) < stay and deck:   # «глупый» дилер тянет дольше — шанс на его перебор
        dealer.append(deck.pop())
    await _bj_finish(cb, state, session, player, dealer)


# ---------------------------------------------------------------------------
async def bump_games_won(session: AsyncSession, tg_id: int) -> None:
    """Счётчик побед для ачивки games_won_10 («Игумен»)."""
    users = UserRepository(session)
    new_val = await users.bump_stat(tg_id, "games_won", 1)
    await AchievementService(session).check(tg_id, {"games_won": new_val})
