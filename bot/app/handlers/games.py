from __future__ import annotations

import random

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError
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


def _chat_of(cb: CallbackQuery) -> int | None:
    """chat_id для стека навигации: «Назад» строится по истории этого чата."""
    return cb.message.chat.id if cb.message else None

class Games(StatesGroup):
    guessing = State()
    rps = State()
    blackjack = State()

async def _get_pet(session: AsyncSession, tg_id: int):
    return await PetRepository(session).get_by_user(tg_id)

@router.callback_query(F.data == "pet:games")
async def games_screen(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await state.clear()
        await safe_edit_or_answer(cb.message, "🥚 Сначала заведи питомца (/start).",
                                  reply_markup=pet_hub(2))
        await cb.answer()
        return
    set_pet_page(cb.message.chat.id, 2)
    sp = SPECIES_DATA.get(_species_key(pet), SPECIES_DATA["cat"])
    await state.clear()
    await safe_edit_or_answer(cb.message, 
        f"🎮 <b>Игровая с {pet.name}</b> {sp['emoji']}\n\n"
        "• 🔢 <i>Угадай число</i> — 🧠 интеллект сужает подсказку\n"
        "• ✂️ <i>Камень-ножницы-бумага</i> — честный рандом\n"
        "• 🃏 <i>Двадцать одно</i> — набери ≤21; 🧠 интеллект делает дилера «мягче»\n\n"
        "Победа: +15 XP и море счастья. Поражение всё равно даёт опыт!",
        reply_markup=games_menu(_chat_of(cb)),
    )
    await cb.answer()

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
        reply_markup=guess_hint_keyboard(lo, hi, _chat_of(cb)),
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
                               reply_markup=games_menu(_chat_of(cb)))
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
    await message.answer(f"{result}{hint}",
                         reply_markup=games_menu(message.chat.id),
                         parse_mode="HTML")

@router.callback_query(F.data == "game:rps")
async def start_rps(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    await state.set_state(Games.rps)
    await safe_edit_or_answer(cb.message, 
        "✂️ <b>Камень-ножницы-бумага!</b>\n\n"
        f"{pet.name} уже выбрал ход (честный рандом). Выбирай свой — откроемся одновременно.",
        reply_markup=rps_keyboard(_chat_of(cb)),
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
        reply_markup=games_menu(_chat_of(cb)),
    )
    from app.utils.fx import apply_effect
    await apply_effect(cb, "win" if won else ("play" if draw else "lose"),
                       toast_override=outcome[:200])

BJ_DECK = [(r, s) for r in range(2, 11) for s in ("♠", "♥", "♦", "♣")]

BJ_LABELS = {2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9", 10: "10", 11: "Т"}

def _bj_norm(cards) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for c in cards or []:
        try:
            out.append((int(c[0]), str(c[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out

def _bj_value(cards: list[tuple[int, str]]) -> int:
    total = 0
    aces = 0
    for rank, _suit in _bj_norm(cards):
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
    cards = _bj_norm(cards)
    if hidden and cards:
        return f"{_card_str([cards[0]])} + 🂠"
    return _card_str(cards)

def _card_str(cards: list[tuple[int, str]]) -> str:
    return " ".join(BJ_LABELS.get(r, "?") + s for r, s in _bj_norm(cards)) or "—"

@router.callback_query(F.data == "game:blackjack")
async def start_blackjack(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    rng = random.Random()
    deck = BJ_DECK[:]
    rng.shuffle(deck)
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
        reply_markup=twentyone_keyboard(_chat_of(cb)),
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
        reply_markup=games_menu(_chat_of(cb)),
    )
    try:
        await cb.answer(outcome[:200])
    except TelegramAPIError:
        pass

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
        reply_markup=twentyone_keyboard(_chat_of(cb)),
    )
    await cb.answer(f"🃏 У тебя {pv} · в колоде ещё {len(deck)} карт")

@router.callback_query(Games.blackjack, F.data == "bj:stand")
async def bj_stand(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    deck, player = list(data.get("deck") or []), list(data.get("player") or [])
    dealer = list(data.get("dealer") or [])
    stay = int(data.get("stay", 17))
    while _bj_value(dealer) < stay and deck:
        dealer.append(deck.pop())
    await _bj_finish(cb, state, session, player, dealer)

async def bump_games_won(session: AsyncSession, tg_id: int) -> None:
    users = UserRepository(session)
    new_val = await users.bump_stat(tg_id, "games_won", 1)
    await AchievementService(session).check(tg_id, {"games_won": new_val})
