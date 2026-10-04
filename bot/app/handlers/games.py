from __future__ import annotations

import random

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import PetRepository, UserRepository
from app.keyboards.inline import (
    games_menu, inline_back_kb, pet_hub,
)
from app.services.achievements import AchievementService
from app.utils.safe_edit import safe_edit_or_answer
from app.handlers.tamagotchi import set_pet_page, _deny
from app.services.tamagotchi import SPECIES_DATA, TamagotchiService, _species_key

router = Router(name="games")

RPS_EMOJI = {"rock": "🪨", "scissors": "✂️", "paper": "📄"}

# Классические правила КНБ: 🪨 бьёт ✂️, ✂️ режут 📄, 📄 накрывает 🪨.
# RPS_BEATS[hand] = ход, который ЭТОТ hand побеждает.
RPS_BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}


def rps_outcome(mine: str, theirs: str) -> str:
    """'win' | 'lose' | 'draw' с точки зрения игрока (mine)."""
    if mine == theirs:
        return "draw"
    return "win" if RPS_BEATS[mine] == theirs else "lose"


def _chat_of(cb: CallbackQuery) -> int | None:
    """chat_id для стека навигации: «Назад» строится по истории этого чата."""
    return cb.message.chat.id if cb.message else None

class Games(StatesGroup):
    guessing = State()
    rps = State()
    blackjack = State()

async def _get_pet(session: AsyncSession, tg_id: int):
    return await PetRepository(session).get_by_user(tg_id)


GAMES_SCREEN_TEXT = (
    "🎮 <b>Игровая с {name}</b> {emoji}\n\n"
    "• 🔢 <i>Угадай число</i> — 🧠 интеллект сужает подсказку\n"
    "• ✂️ <i>Камень-ножницы-бумага</i> — честный рандом\n"
    "• 🃏 <i>Двадцать одно</i> — набери ≤21; 🧠 интеллект делает дилера «мягче»\n\n"
    "Победа: +15 XP и море счастья. Поражение всё равно даёт опыт!"
)


async def _games_screen_render(cb: CallbackQuery, pet) -> None:
    """Экран меню игр (единый для входа и выхода из мини-игр)."""
    sp = SPECIES_DATA.get(_species_key(pet), SPECIES_DATA["cat"])
    await safe_edit_or_answer(
        cb.message, GAMES_SCREEN_TEXT.format(name=pet.name, emoji=sp["emoji"]),
        reply_markup=games_menu(_chat_of(cb)))
    await cb.answer()


async def _game_entry_guard(cb: CallbackQuery, state: FSMContext,
                            session: AsyncSession):
    """Единый страж входа в игры (экран меню и все три мини-игры).

    Стиль отказа — всплывающий alert-тост (`_deny`), экран не трогаем.
    Истёкшую прогулку НЕ молча стираем: отдаём её на «сбор» общему пути
    `_collect_walk_result` (награды монеты/XP запишет `_game_outcome`,
    как это делает `_after_action` в хабе питомца) — иначе игрок терял
    награду прогулки, а кнопка «Прогулка» работала со сбросом состояния.
    """
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await state.clear()
        await safe_edit_or_answer(cb.message, "🥚 Сначала заведи питомца (/start).",
                                  reply_markup=pet_hub(2))
        await cb.answer()
        return None
    set_pet_page(_chat_of(cb) or 0, 2)
    await state.clear()
    svc = TamagotchiService(session)
    deny = svc.state_deny(pet, "game")
    if deny:
        await _deny(cb, deny)
        return None
    return svc, pet


async def _game_outcome(cb: CallbackQuery, session: AsyncSession, pet,
                        result_text: str, won: bool, draw: bool = False,
                        *, kind: str = "", meta: dict | None = None) -> None:
    """Единый обработчик итога мини-игры (все три игры идут через него).

    Порядок один и тот же: собрать просроченную прогулку (награды!) →
    показать исход + карточку статов → тост `apply_effect` (реакции бот
    НЕ ставит — это было багом) → log_action → commit.
    """
    svc = TamagotchiService(session)
    prefix = ""
    res = _collect_walk_result(svc, pet, session)
    if res:
        wtext, coins, xp = res
        pet.walk_until = None
        pet.walk_start_at = None
        if coins:
            user = await UserRepository(session).get(cb.from_user.id)
            if user:
                user.coins += coins
        await svc.add_pet_xp(pet, xp)
        await PetRepository(session).log_action(pet.id, "walk_done", value=coins)
        prefix = f"{wtext}\n\n"
    await safe_edit_or_answer(cb.message,
        f"{prefix}{result_text}\n\n" + await svc.render_async(pet),
        reply_markup=games_menu(_chat_of(cb)))
    from app.utils.fx import apply_effect
    await apply_effect(cb, "win" if won else ("play" if draw else "lose"),
                       toast_override=result_text[:200])
    await PetRepository(session).log_action(
        pet.id, "game", value=int(won), meta={"kind": kind, **(meta or {})})
    if won:
        await bump_games_won(session, cb.from_user.id)
    await session.commit()


@router.callback_query(F.data == "pet:games")
async def games_screen(cb: CallbackQuery, state: FSMContext,
                       session: AsyncSession) -> None:
    """🎮 Игровая — экран выбора мини-игры (тот же страж и тот же стиль отказа)."""
    entry = await _game_entry_guard(cb, state, session)
    if entry is None:
        return
    _, pet = entry
    await _games_screen_render(cb, pet)


@router.callback_query(F.data.startswith("game:"))
async def game_noop_guard(cb: CallbackQuery) -> None:
    """Защита от «мёртвых» кнопок: любой неизвестный game:-колбэк просто
    отвечает на тап (иначе Telegram показывает «кнопка неактивна»)."""
    await cb.answer()


@router.callback_query(F.data == "game:exit")
async def game_exit(cb: CallbackQuery, state: FSMContext,
                    session: AsyncSession) -> None:
    """⬅️ Выйти из мини-игры → экран меню игр (снимает состояние FSM)."""
    await state.clear()
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await safe_edit_or_answer(cb.message, "🥚 Сначала заведи питомца (/start).",
                                  reply_markup=pet_hub(2))
        return await cb.answer()
    set_pet_page(_chat_of(cb) or 0, 2)
    await _games_screen_render(cb, pet)


@router.callback_query(F.data == "game:guess")
async def start_guess(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    entry = await _game_entry_guard(cb, state, session)
    if entry is None:
        return
    svc, pet = entry
    secret, (lo, hi) = svc.guess_range(pet)
    await state.set_state(Games.guessing)
    await state.update_data(secret=secret, lo=lo, hi=hi)
    await safe_edit_or_answer(cb.message, 
        f"🔢 Питомец загадал число от 1 до 20. Друзья шепчут, что оно в диапазоне "
        f"<b>{lo}…{hi}</b> (чем умнее питомец, тем точнее подсказка!).\n\n"
        "Нажми кнопку-вариант или напиши своё число сообщением:",
        reply_markup=_guess_kb(lo, hi, _chat_of(cb)),
    )
    await cb.answer()


def _guess_kb(lo: int, hi: int, chat_id: int | None) -> InlineKeyboardMarkup:
    """Варианты чисел + ЯВНЫЙ выход из игры (кнопка «Назад» по стеку может
    не построиться без Redis — гарантируем путь наружу)."""
    mid = (lo + hi) // 2
    rows = [[(str(n), f"guess:{n}") for n in (lo, mid, hi)]]
    return inline_back_kb("games", chat_id=chat_id, extra_rows=rows)


def _rps_kb(chat_id: int | None) -> InlineKeyboardMarkup:
    rows = [[("🪨 Камень", "rps:rock"), ("✂️ Ножницы", "rps:scissors"),
             ("📄 Бумага", "rps:paper")]]
    return inline_back_kb("games", chat_id=chat_id, extra_rows=rows)


def _bj_kb(chat_id: int | None) -> InlineKeyboardMarkup:
    rows = [[("➕ Ещё карту", "bj:hit"), ("✋ Хватит", "bj:stand")]]
    return inline_back_kb("games", chat_id=chat_id, extra_rows=rows)

@router.callback_query(Games.guessing, F.data.startswith("guess:"))
async def do_guess_cb(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    secret = int(data.get("secret", -1))
    try:
        guess = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        return await cb.answer()
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await state.clear()
        return await cb.answer()
    svc = TamagotchiService(session)
    won = guess == secret
    result = await svc.play(pet, won)
    await state.clear()
    hint = "" if won else f" Это было число <b>{secret}</b>."
    await _game_outcome(cb, session, pet, f"{result}{hint}", won,
                        kind="guess", meta={"guess": guess})

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
    await session.commit()


@router.callback_query(F.data == "game:rps")
async def start_rps(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    entry = await _game_entry_guard(cb, state, session)
    if entry is None:
        return
    _, pet = entry
    await state.set_state(Games.rps)
    # Ход питомца ЖЕРЕБУЕТСЯ ЗАРАНЕЕ и хранится в FSM: игрок выбирает
    # вслепую («синхронное раскрытие»), а не получает ответ постфактум.
    pet_hand = random.choice(list(RPS_EMOJI))
    await state.update_data(pet_hand=pet_hand)
    await safe_edit_or_answer(cb.message,
        "✂️ <b>Камень-ножницы-бумага!</b>\n\n"
        f"{pet.name} уже тайно выбрал свой ход 🤫 (честный рандом).\n"
        "Выбирай свой — откроемся одновременно.\n\n"
        "Правила: 🪨 бьёт ✂️ · ✂️ режет 📄 · 📄 накрывает 🪨",
        reply_markup=_rps_kb(_chat_of(cb)),
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
    data = await state.get_data()
    theirs = data.get("pet_hand")
    if theirs not in RPS_EMOJI:
        theirs = random.choice(list(RPS_EMOJI))
    # Единый источник истины по правилам КНБ (см. RPS_BEATS): побеждает тот,
    # чей ход бьёт ход соперника. Никакого «угадывания» — исход полностью
    # определяется самими ходами: ✂️ режут 📄, значит выиграл показавший ✂️.
    outcome = rps_outcome(mine, theirs)
    won = outcome == "win"
    draw = outcome == "draw"
    svc = TamagotchiService(session)
    result = await svc.play(pet, won)
    await state.clear()
    # Причина результата — в самих ходах (🪨 > ✂️ > 📄 > 🪨), поэтому строка
    # с ходами объясняет ВСЁ. Ниже — только награда от питомца (без повтора
    # слова «Победа») и карточка статов.
    if draw:
        line = f"Ты: {RPS_EMOJI[mine]} · {pet.name}: {RPS_EMOJI[theirs]} — 🤝 ничья, одинаковые ходы."
    elif won:
        line = (f"Ты: {RPS_EMOJI[mine]} · {pet.name}: {RPS_EMOJI[theirs]} — "
                f"✅ твой ход бьёт: {RPS_EMOJI[mine]} побеждает {RPS_EMOJI[theirs]}.")
    else:
        line = (f"Ты: {RPS_EMOJI[mine]} · {pet.name}: {RPS_EMOJI[theirs]} — "
                f"❌ ход питомца бьёт: {RPS_EMOJI[theirs]} побеждает {RPS_EMOJI[mine]}.")
    reward = ("Питомец в восторге!" if won else
              "Ничья — питомец довольно урчит." if draw else
              "В следующий раз повезёт больше!")
    # В строке выше уже написано, ЧЬЙ ход победил — не дублируем слово
    # «Победа» из общего результата play(): оставляем только награду (+XP).
    res_line = result.split("!", 1)[-1].strip() if won and "!" in result else result
    await _game_outcome(cb, session, pet, f"{line}\n{reward} {res_line}",
                        won, draw, kind="rps",
                        meta={"mine": mine, "theirs": theirs})

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
    entry = await _game_entry_guard(cb, state, session)
    if entry is None:
        return
    _, pet = entry
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
        reply_markup=_bj_kb(_chat_of(cb)),
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
    text = (f"Твои: <b>{_bj_render(player)}</b> ({pv}) · "
            f"{pet.name}: <b>{_bj_render(dealer)}</b> ({dv})\n{outcome}\n\n{result}")
    await _game_outcome(cb, session, pet, text, won, draw=(pv == dv),
                        kind="blackjack", meta={"player": pv, "dealer": dv})

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
        reply_markup=_bj_kb(_chat_of(cb)),
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
