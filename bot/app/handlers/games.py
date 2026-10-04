from __future__ import annotations

import base64
import json
import random
import zlib

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
        # Единый стиль отказа: всплывающий alert (как у «Покормить» во сне),
        # экран не перерисовываем.
        return await _deny(cb, "🥚 Сначала заведи питомца — /start")
    set_pet_page(_chat_of(cb) or 0, 2)
    await state.clear()
    svc = TamagotchiService(session)
    deny = svc.state_deny(pet, "game")
    if deny:
        await _deny(cb, deny)
        return None
    return svc, pet


# Единственные тексты, которыми play() сообщает о РЕАЛЬНОЙ победе/поражении.
# Любой другой возврат — отказ (сон, прогулка, критическое состояние,
# усталость, кулдаун): награду не начисляем и в статистику не пишем.
_PLAY_REWARD_MARKERS = ("🎉 Победа!", "🙂 Не повезло")


def _outcome_is_denial(text: str) -> bool:
    """Итог svc.play(), при котором НАГРАДУ засчитывать нельзя."""
    return not text.startswith(_PLAY_REWARD_MARKERS)


def _register_game_screen(chat_id: int | None, token: str | None = None) -> None:
    """Снимок активного игрового экрана чата — для текстовых ходов
    угадайки (см. do_guess_msg): входящий текст несёт только номер, а
    секрет партии лежит в кнопке «🔄 Новая игра» (guess:new:<секрет>)."""
    if chat_id is None:
        return
    from app.handlers.tamagotchi import get_last_game_screen, set_last_game_screen
    if token is None and get_last_game_screen(chat_id) != "game:exit":
        # Итоговый экран содержит «⬅️ Выйти из игры» — она и станет
        # единственным живым контекстом партии: тап по старой кнопке
        # числа после завершения игры вернёт игрока в меню, а не будет
        # молча проигнорирован.
        token = "game:exit"
    set_last_game_screen(chat_id, token)


async def _game_outcome(cb: CallbackQuery, session: AsyncSession, pet,
                        result_text: str, won: bool, draw: bool = False,
                        *, kind: str = "", meta: dict | None = None) -> None:
    """Единый обработчик итога мини-игры (все три игры идут через него).

    Порядок один и тот же: собрать просроченную прогулку (награды!) →
    показать исход + карточку статов → log_action → commit. Ровно ОДИН
    cb.answer() делает вызывающий хендлер в конце (Telegram принимает
    только один ответ на тап). Реакции бот НЕ ставит — это было багом.
    """
    svc = TamagotchiService(session)
    _register_game_screen(_chat_of(cb), None)   # партия окончена
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
    if not _outcome_is_denial(result_text):
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


_GAME_KNOWN_CB = {"game:exit", "game:guess", "game:rps", "game:blackjack"}


@router.callback_query(F.data.startswith("game:") & ~F.data.in_(list(_GAME_KNOWN_CB)))
async def game_noop_guard(cb: CallbackQuery) -> None:
    """Защита от «мёртвых» кнопок: любой НЕИЗВЕСТНЫЙ game:-колбэк просто
    отвечает на тап (иначе Telegram показывает «кнопка неактивна»).

    ВАЖНО: известные кнопки входа в игры ИСКЛЮЧЕНЫ из фильтра. Раньше этот
    guard стоял до start_guess/start_rps/start_blackjack и матчил их данные
    тоже — aiogram останавливался на первом совпадении, и при нажатии любой
    игры «ничего не происходило» (только гасился спиннер)."""
    await cb.answer()


@router.callback_query(F.data == "game:exit")
async def game_exit(cb: CallbackQuery, state: FSMContext,
                    session: AsyncSession) -> None:
    """⬅️ Выйти из мини-игры → экран меню игр (снимает состояние FSM)."""
    await state.clear()
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await _deny(cb, "🥚 Сначала заведи питомца — /start")
    set_pet_page(_chat_of(cb) or 0, 2)
    _register_game_screen(_chat_of(cb), None)
    await _games_screen_render(cb, pet)


@router.callback_query(F.data == "game:guess")
async def start_guess(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    entry = await _game_entry_guard(cb, state, session)
    if entry is None:
        return
    svc, pet = entry
    secret, (lo, hi) = svc.guess_range(pet)
    await state.clear()   # игра не требует FSM: контекст хода живёт в кнопках
    await safe_edit_or_answer(cb.message, 
        f"🔢 Питомец загадал число от 1 до 20. Друзья шепчут, что оно в диапазоне "
        f"<b>{lo}…{hi}</b> (чем умнее питомец, тем точнее подсказка!).\n\n"
        "Нажми кнопку-вариант или напиши своё число сообщением:",
        reply_markup=_guess_kb(secret, lo, hi, _chat_of(cb)),
    )
    _register_game_screen(_chat_of(cb), f"guess:new:{secret}")
    await cb.answer()


def _guess_kb(secret: int, lo: int, hi: int, chat_id: int | None) -> InlineKeyboardMarkup:
    """Варианты чисел + кнопка-контекст + ЯВНЫЙ выход из игры.

    Секрет спрятан в callback_data кнопки «🔄 Новая игра»: она есть ТОЛЬКО
    на активном экране угадайки, поэтому ход обрабатывается без FSM и
    переживает перезапуск бота. На итоговом экране этой кнопки нет — тап
    по старой кнопке числа вернёт игрока в меню игр."""
    mid = (lo + hi) // 2
    uniq = sorted({lo, mid, hi})   # при узком диапазоне lo==mid — дубликаты недопустимы
    rows = [[(str(n), f"guess:{n}") for n in uniq],
            [("🔄 Новая игра", f"guess:new:{secret}")]]
    return inline_back_kb("games", chat_id=chat_id, extra_rows=rows)


def _bj_state_b64(deck, player, dealer, stay: int) -> str:
    """Снимок партии в callback_data кнопки (замена FSM для блэкджека).

    Состояние игры живёт на экране: переживает перезапуск бота и потерю
    FSM-storage. Подпись (crc32) защищает от ручного редактирования кнопок;
    невалидный снимок = «партия закончилась» → возврат в меню игр.
    """
    payload = json.dumps([deck, player, dealer, stay], separators=(",", ":"))
    blob = base64.urlsafe_b64encode(zlib.compress(payload.encode())).decode().rstrip("=")
    return f"{blob}.{zlib.crc32(blob.encode()) & 0xFFFFFFFF:x}"


def _bj_load_state(data: str):
    """('ok', deck, player, dealer, stay) | ('stale',) | ('bad',).

    Формат кнопки: «bj:<действие>:<токен>». Токен парсится справа налево
    (rsplit), а НЕ split(":",1)[1]: раньше из-за split'а в токен попадало
    слово действия и снимок никогда не совпадал с подписью — любая карта
    «не работала», игра выглядела зависшей.
    """
    parts = data.rsplit(":", 1)
    token = parts[1] if len(parts) == 2 else ""
    blob, _, sig = token.rpartition(".")
    if not blob or len(token) > 4096:
        return ("stale",)
    if sig != f"{zlib.crc32(blob.encode()) & 0xFFFFFFFF:x}":
        return ("bad",)
    try:
        raw = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))
        deck, player, dealer, stay = json.loads(zlib.decompress(raw))
        return ("ok", [tuple(c) for c in deck], [tuple(c) for c in player],
                [tuple(c) for c in dealer], int(stay))
    except Exception:  # noqa: BLE001 — любая порча снимка = stale
        return ("stale",)


def _bj_kb(state_token: str, chat_id: int | None) -> InlineKeyboardMarkup:
    rows = [[("➕ Ещё карту", f"bj:hit:{state_token}"),
             ("✋ Хватит", f"bj:stand:{state_token}")]]
    return inline_back_kb("games", chat_id=chat_id, extra_rows=rows)


# ── Единый вход кнопок мини-игр (без FSM!) ────────────────────────────────
# Архитектура игр (финальная, 2026-10). Раньше ходы были обвешаны
# FSM-фильтрами (Games.guessing/rps/blackjack) + «стражами потерянного
# состояния», которые стояли в роутере РАНЬШЕ целевых хендлеров. Любая
# ошибка в порядке объявления / возврате guard'а / состоянии FSM-storage
# приводила к симптому «игра началась, а кнопки ходов молчат»: update
# либо гасился guard'ом, либо не матчил ни один хендлер.
#
# Теперь FSM для ХОДОВ не используется вообще. Каждый callback самодостаточен:
#   1) определяет игру по префиксу данных кнопки («rps:rock» → rps);
#   2) берёт контекст хода из message.reply_markup (он всегда актуален —
#      экран игры перерисовывается на каждом ходе);
#   3) если контекста нет (тап по кнопке старой/завершённой игры) —
#      мягко возвращает игрока в меню игр вместо тишины.
# Никаких guard'ов до хендлеров, никаких зависимостей от Redis/MemoryStorage
# для продолжения игры — единственный источник истины для хода = сама
# кнопка под пальцем пользователя.
_STALE_GAME_HINTS = {
    "guess": "🔢 Угадай число", "rps": "✂️ Камень-ножницы-бумага",
    "bj": "🃏 Двадцать одно",
}


def _find_cb(buttons, suffix: str | None = None) -> str | None:
    """Первый callback_data среди inline-кнопок (или с нужным суффиксом)."""
    for row in buttons or []:
        for b in getattr(row, "buttons", []) or []:
            d = b.callback_data or ""
            if suffix is None or d.endswith(suffix):
                return d
    return None


def _game_prefix(data: str) -> str | None:
    return next((p for p in _STALE_GAME_HINTS if data.startswith(f"{p}:")), None)


async def _stale_game_screen(cb: CallbackQuery, state: FSMContext,
                             session: AsyncSession, prefix: str) -> None:
    """Тап по кнопке завершённой игры → меню игр (явный ответ, не тишина)."""
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await cb.answer("🥚 Сначала заведи питомца (/start)", show_alert=True)
        if cb.message:
            await safe_edit_or_answer(cb.message, "🥚 Сначала заведи питомца (/start).",
                                      reply_markup=pet_hub(2))
        return
    set_pet_page(_chat_of(cb) or 0, 2)
    await state.clear()
    _register_game_screen(_chat_of(cb), None)
    await cb.answer(f"⏳ Игра «{_STALE_GAME_HINTS[prefix]}» закончилась — начинай заново")
    if cb.message:
        await safe_edit_or_answer(
            cb.message,
            f"⏳ Игра «{_STALE_GAME_HINTS[prefix]}» уже закончилась. "
            "Начинай заново — выбор игр ниже 👇",
            reply_markup=games_menu(_chat_of(cb)))


@router.callback_query(F.data.startswith("guess:new:"))
async def guess_new(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """🔄 Новая партия угадайки без возврата в меню игр."""
    entry = await _game_entry_guard(cb, state, session)
    if entry is None:
        return
    svc, pet = entry
    secret, (lo, hi) = svc.guess_range(pet)
    await safe_edit_or_answer(cb.message,
        f"🔢 {pet.name} загадал новое число от 1 до 20. Подсказка: диапазон "
        f"<b>{lo}…{hi}</b>.\n\nНажми вариант или напиши своё число:",
        reply_markup=_guess_kb(secret, lo, hi, _chat_of(cb)))
    _register_game_screen(_chat_of(cb), f"guess:new:{secret}")
    await cb.answer("🔢 Новое число загадано!")


@router.callback_query(F.data.startswith("guess:"))
async def do_guess_cb(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    try:
        guess = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        return await cb.answer()
    # Секрет живёт в кнопке «🔄 Новая игра» текущего экрана (она видна
    # только во время активной партии) — FSM для хода не нужен.
    btn = _find_cb(cb.message.reply_markup.inline_keyboard
                   if cb.message and cb.message.reply_markup else None,
                   suffix=":new")
    if btn is None:
        return await _stale_game_screen(cb, state, session, "guess")
    _, _, secret_s = btn.partition(":")
    try:
        secret = int(secret_s)
    except ValueError:
        return await _stale_game_screen(cb, state, session, "guess")
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await state.clear()
        return await _deny(cb, "🥚 Сначала заведи питомца — /start")
    svc = TamagotchiService(session)
    won = guess == secret
    result = await svc.play(pet, won)
    await state.clear()
    hint = "" if won else f" Это было число <b>{secret}</b>."
    await _game_outcome(cb, session, pet, f"{result}{hint}", won,
                        kind="guess", meta={"guess": guess})
    await cb.answer("🎯 Ты выбрал " + str(guess))

@router.message(F.text & F.text.strip().isdigit())
async def do_guess_msg(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Числовое сообщение = ход в угадайку, если на экране этого чата есть
    активная партия (кнопка guess:new). Без FSM-состояний и без риска
    перехватить обычный переписочный ввод."""
    txt = message.text.strip()
    guess = int(txt)
    # Числа вне диапазона игры — просто сообщение, не ход (защита от
    # ложных срабатываний на обычный цифровой ввод пользователя).
    if not 1 <= guess <= 20:
        return
    from app.handlers.tamagotchi import get_last_game_screen
    token = get_last_game_screen(message.chat.id)
    if not token or not token.startswith("guess:new:"):
        return   # активной партии нет — это просто сообщение, не ход
    try:
        secret = int(token.split(":")[2])
    except (ValueError, IndexError):
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
    await state.clear()   # игра без FSM: ход питомца зашит в кнопки экрана
    # Ход питомца ЖЕРЕБУЕТСЯ ЗАРАНЕЕ и прячется в callback_data кнопок
    # выбора (например «rps:rock:p»): игрок выбирает вслепую («синхронное
    # раскрытие»), а не получает ответ постфактум. Контекст живёт на самом
    # экране — переживает перезапуск бота и потерю FSM-storage.
    pet_hand = random.choice(list(RPS_EMOJI))
    await safe_edit_or_answer(cb.message,
        "✂️ <b>Камень-ножницы-бумага!</b>\n\n"
        f"{pet.name} уже тайно выбрал свой ход 🤫 (честный рандом).\n"
        "Выбирай свой — откроемся одновременно.\n\n"
        "Правила: 🪨 бьёт ✂️ · ✂️ режет 📄 · 📄 накрывает 🪨",
        reply_markup=_rps_kb(pet_hand, _chat_of(cb)),
    )
    _register_game_screen(_chat_of(cb))
    await cb.answer()

def _rps_kb(pet_hand: str, chat_id: int | None) -> InlineKeyboardMarkup:
    """Кнопки хода, в callback_data которых спрятан тайный ход питомца:
    «rps:<мой ход>:<ход питомца>». Это замена FSM — ход обрабатывается
    самодостаточным колбэком."""
    rows = [[(label, f"rps:{hand}:{pet_hand}") for hand, label in RPS_EMOJI.items()]]
    return inline_back_kb("games", chat_id=chat_id, extra_rows=rows)

@router.callback_query(F.data.startswith("rps:"))
async def play_rps(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    parts = (cb.data or "").split(":")
    mine = parts[1] if len(parts) > 1 else ""
    theirs = parts[2] if len(parts) > 2 else ""
    if mine not in RPS_EMOJI:
        return await cb.answer()
    if theirs not in RPS_EMOJI:
        # Тап по кнопке старой партии (питомец уже раскрылся) — в меню игр.
        return await _stale_game_screen(cb, state, session, "rps")
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await state.clear()
        return await _deny(cb, "🥚 Сначала заведи питомца — /start")
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
    await cb.answer(f"Твой ход: {RPS_EMOJI[mine]}")

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
    await state.clear()   # игра без FSM: снимок партии — в кнопках экрана
    token = _bj_state_b64(deck, player, dealer, dealer_stay)
    await safe_edit_or_answer(cb.message,
        f"🃏 <b>Двадцать одно!</b> {pet.name} — дилер.\n\n"
        f"Твои карты: <b>{_bj_render(player)}</b> ({_bj_value(player)})\n"
        f"Карты дилера: <b>{_bj_render(dealer, hidden=True)}</b>\n\n"
        "«Ещё» — взять карту, «Хватит» — остановиться. Больше 21 — перебор!",
        reply_markup=_bj_kb(token, _chat_of(cb)),
    )
    _register_game_screen(_chat_of(cb))
    await cb.answer()

async def _bj_finish(cb: CallbackQuery, state: FSMContext, session: AsyncSession,
                     player: list, dealer: list) -> None:
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await state.clear()
        return await _deny(cb, "🥚 Сначала заведи питомца — /start")
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
    await cb.answer(f"🃏 У тебя {pv} · у дилера {dv}")

@router.callback_query(F.data.startswith("bj:hit:"))
async def bj_hit(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    loaded = _bj_load_state(cb.data or "")
    if loaded[0] != "ok":
        return await _stale_game_screen(cb, state, session, "bj")
    _, deck, player, dealer, stay = loaded
    if not deck:
        await state.clear()
        return await cb.answer("Колода кончилась — начни игру заново", show_alert=True)
    player.append(deck.pop())
    pv = _bj_value(player)
    if pv >= 21:
        return await _bj_finish(cb, state, session, player, dealer)
    token = _bj_state_b64(deck, player, dealer, stay)
    await safe_edit_or_answer(cb.message,
        f"🃏 Твои карты: <b>{_bj_render(player)}</b> ({pv})\n"
        f"Карты дилера: <b>{_bj_render(dealer, hidden=True)}</b>\n\n"
        "Ещё или хватит?",
        reply_markup=_bj_kb(token, _chat_of(cb)),
    )
    _register_game_screen(_chat_of(cb))
    await cb.answer(f"🃏 У тебя {pv} · в колоде ещё {len(deck)} карт")

@router.callback_query(F.data.startswith("bj:stand:"))
async def bj_stand(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    loaded = _bj_load_state(cb.data or "")
    if loaded[0] != "ok":
        return await _stale_game_screen(cb, state, session, "bj")
    _, deck, player, dealer, stay = loaded
    while _bj_value(dealer) < stay and deck:
        dealer.append(deck.pop())
    await _bj_finish(cb, state, session, player, dealer)

async def bump_games_won(session: AsyncSession, tg_id: int) -> None:
    users = UserRepository(session)
    new_val = await users.bump_stat(tg_id, "games_won", 1)
    await AchievementService(session).check(tg_id, {"games_won": new_val})
