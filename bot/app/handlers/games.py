from __future__ import annotations

"""Мини-игры с питомцем — финальная архитектура (полностью переписана).

Почему переписано: пять итераций «фиксов» не помогали, потому что лечили
следствия. Реальные причины молчащих кнопок хода:

1) FSM-фильтры на хендлерах ходов. Состояние терялось при рестарте бота /
   перезагрузке приложения — тап по 🪨/✂️/📄 или «Ещё карту» не матчил НИ
   ОДИН хендлер и пропадал в пустоту.
2) Стопка guard'ов («стражей») ДО целевых хендлеров: любой лишний возврат
   (в т.ч. coroutine вместо falsy) останавливал диспетчеризацию aiogram —
   кнопки гасились молча.
3) Ходы зависели от message.reply_markup / снимков экрана: после
   перезагрузки контекст партии было неоткуда взять.

Финальные правила этой архитектуры (нарушать нельзя):
• НИКАКОГО FSM для игр — ни состояний, ни фильтров, ни guard'ов перед
  хендлерами ходов. Единственный источник истины хода = сам callback_data
  под пальцем пользователя (секрет угадайки, тайный ход питомца в КНБ,
  снимок колоды в блэкджеке). Это переживает рестарты бота и приложения.
• РОВНО ОДИН cb.answer() на тап во всех путях каждого хендлера (второй
  ответ = TelegramBadRequest QUERY_ID_INVALID → обработчик падал, и кнопка
  выглядела «неактивной»). Никаких текстов в answer(): отказы идут через
  show_alert (_deny), нормальный ход — пустой answer(), текст итога живёт
  на экране.
• Единый страж входа _game_entry_guard: нет питомца / спит / гуляет —
  всплывающий alert-тост (та же стилистика, что у «Покормить» во сне),
  экран не трогается. Награда начисляется ТОЛЬКО за реальную
  победу/поражение svc.play().
• Все итоги идут через единый _finish_game: svc.play → собрать
  просроченную прогулку (с наградами!) → edit экрана (при ошибке edit —
  новое сообщение, никогда не бросаем) → log_action → commit.
• Любая «мёртвая» кнопка (старая партия, битый токен) получает ЯВНЫЙ ответ
  и возврат в меню игр — никогда тишина.
"""

import base64
import json
import logging
import os
import random
import secrets
import time
import zlib

from cachetools import TTLCache

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import PetRepository, UserRepository
from app.i18n import t
from app.utils.local_time import now as local_now
from app.keyboards.inline import games_menu, inline_back_kb, pet_hub
from app.services.achievements import AchievementService
from app.utils.safe_edit import safe_edit_or_answer
from app.handlers.tamagotchi import set_pet_page, _deny
from app.services.tamagotchi import SPECIES_DATA, TamagotchiService, _species_key

router = Router(name="games")
logger = logging.getLogger(__name__)

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


async def _get_pet(session: AsyncSession, tg_id: int):
    return await PetRepository(session).get_by_user(tg_id)


GAMES_SCREEN_TEXT = (
    "🎮 <b>Игровая с {name}</b> {emoji}\n\n"
    "• 🔢 <i>Угадай число</i> — 🧠 интеллект сужает подсказку\n"
    "• ✂️ <i>Камень-ножницы-бумага</i> — честный рандом\n"
    "• 🃏 <i>Двадцать одно</i> — набери ≤21; 🧠 интеллект делает дилера «мягче»\n\n"
    "Победа: +15 XP и море счастья. Поражение всё равно даёт опыт!\n"
    "⛔ Не играем, пока питомец спит 😴 или гуляет 🚶."
)


async def _games_screen_render(cb: CallbackQuery, pet) -> None:
    """Экран меню игр (единый для входа и выхода из мини-игр)."""
    sp = SPECIES_DATA.get(_species_key(pet), SPECIES_DATA["cat"])
    await safe_edit_or_answer(
        cb.message, GAMES_SCREEN_TEXT.format(name=pet.name, emoji=sp["emoji"]),
        reply_markup=games_menu(_chat_of(cb)))
    await cb.answer()


async def _game_entry_guard(cb: CallbackQuery, session: AsyncSession):
    """Единый страж входа в игры (экран меню и все три мини-игры).

    Стиль отказа — всплывающий alert-тост (`_deny`), экран не трогаем.
    Возвращает (svc, pet) либо None (отказ уже отправлен).
    """
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await _deny(cb, "🥚 Сначала заведи питомца — /start")
        return None
    set_pet_page(_chat_of(cb) or 0, 2)
    svc = TamagotchiService(session)
    deny = svc.state_deny(pet, "game")
    if deny:
        await _deny(cb, deny)
        return None
    return svc, pet


# play() возвращает ЛИБО строку результата («pet.won_game»/«pet.lost_game»),
# ЛИБО строку-отказ (сон, прогулка, критическое состояние, усталость,
# кулдаун). Отказ распознаём СТРУКТУРНО: точным сравнением с полным
# множеством текстов, которые play() в принципе может вернуть как отказ.
# Сравнение по префиксу/эмодзи ломалось бы темами (готика меняет и тексты
# отказов тоже) — здесь совпадение полное, ложных срабатываний нет.
def _outcome_is_denial(text: str) -> bool:
    """True, если svc.play() сообщил об отказе (награду не начисляем).

    Структурная проверка вместо сверки строк: точное совпадение с
    префиксом «🚨» = критическое состояние; иначе — это реальный исход
    игры. Все прочие ветки отказа play() уже отсечены ДО вызова
    svc.play(): сон/прогулка — стражем входа (_game_entry_guard),
    усталость и кулдаун — явными проверками в хендлерах ходов. Поэтому
    детектор намеренно узкий и не зависит от текстов i18n/тем (рассинхрон
    строк больше не может превратить награду в «отказ» или наоборот).
    """
    return text.startswith("🚨")


async def _finish_game(cb: CallbackQuery, session: AsyncSession, pet,
                       won: bool, draw: bool = False, *, kind: str = "",
                       meta: dict | None = None, line: str = "") -> bool:
    """Единый обработчик ИТОГА мини-игры (все три игры идут только через него).

    Порядок один для всех: применить svc.play (награды/штрафы по правилам
    вида, экипировки, погоды) → показать исход + карточку статов →
    log_action → commit. Возвращает True, если итог засчитан, и False при
    отказе play() — вызывающий хендлер тогда НЕ должен отвечать на тап.
    ВАЖНО: этот хелпер НИКОГДА сам не отвечает на callback (кроме отказа,
    где ответ = alert-тост): ровно один cb.answer() на тап гарантирует
    вызывающий хендлер.
    """
    svc = TamagotchiService(session)
    # Явные проверки ДО play(): их тексты — настоящие подсказки игроку,
    # показываем alert-тостом (единый стиль отказов). Кулдаун НЕ проверяем
    # здесь: его текст из i18n («pet.cooldown_feed») говорит про кормёжку и
    # вводит в заблуждение; для игры кулдаун = «запыхался» — свой текст ниже.
    deny = svc.state_deny(pet, "play")
    if deny:
        await _deny(cb, deny)          # сон/прогулка (гонка состояний)
        return False
    if pet.energy < 15:
        await _deny(cb, t("pet.too_tired_play"))
        return False
    ok, wait = svc._check_cooldown(pet, "game", 120, local_now())
    if not ok:
        # РОВНО ОДИН ответ на тап: мягкий toast (не перекрывает экран),
        # награду не начисляем, экран не трогаем.
        await cb.answer(f"⏳ {pet.name} запыхался! Подожди {wait} сек.")
        return False
    result = await svc.play(pet, won)
    if _outcome_is_denial(result):
        # Гонка состояний / критическое состояние: честный отказ тостом
        # (это и есть ЕДИНСТВЕННЫЙ ответ на тап), награду НЕ начисляем,
        # экран не трогаем.
        await _deny(cb, result)
        return False
    prefix = ""
    # Просроченная прогулка собирается с наградами тем же общим путём.
    try:
        from app.handlers.tamagotchi import _collect_walk_result
        res = _collect_walk_result(svc, pet, session)
        if res:
            wtext, coins, xp = res
            pet.walk_until = None
            pet.walk_start_at = None
            if coins:
                # Атомарное начисление — без read-modify-write.
                await UserRepository(session).add_xp_coins(cb.from_user.id, coins=coins)
            await svc.add_pet_xp(pet, xp)
            await PetRepository(session).log_action(pet.id, "walk_done", value=coins)
            prefix = f"{wtext}\n\n"
    except Exception as exc:  # noqa: BLE001 — сбор прогулки не должен ломать итог игры
        logger.debug("Итоги игры: сбор прогулки не удался ({!r}) — пропускаем", exc)
    text = f"{prefix}{line}{result}\n\n{await svc.render_async(pet)}"
    markup = games_menu(_chat_of(cb))
    # Единый безопасный рендер: edit при возможности, иначе новое сообщение;
    # все сетевые ошибки Telegram гасятся внутри — никогда не бросаем,
    # иначе ErrorNotifyMiddleware съест ответ на тап и кнопка будет
    # выглядеть «неактивной».
    if cb.message is not None:
        await safe_edit_or_answer(cb.message, text, reply_markup=markup)
    await PetRepository(session).log_action(
        pet.id, "game", value=int(won),
        meta={"kind": kind, "draw": int(draw), **(meta or {})})
    if won:
        await bump_games_won(session, cb.from_user.id)
    await session.commit()
    return True


async def _stale_game_screen(cb: CallbackQuery, session: AsyncSession,
                             game_hint: str) -> None:
    """Тап по кнопке завершённой/потерянной партии → меню игр.

    Явный ответ + явный экран: «ничего не происходит» в этой архитектуре
    запрещено как класс ошибок.
    """
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await cb.answer("🥚 Сначала заведи питомца (/start)", show_alert=True)
        if cb.message:
            await safe_edit_or_answer(cb.message, "🥚 Сначала заведи питомца (/start).",
                                      reply_markup=pet_hub(2))
        return
    set_pet_page(_chat_of(cb) or 0, 2)
    await cb.answer(f"⏳ Игра «{game_hint}» закончилась — начинай заново")
    if cb.message:
        await safe_edit_or_answer(
            cb.message,
            f"⏳ Игра «{game_hint}» уже закончилась. Начинай заново — выбор игр ниже 👇",
            reply_markup=games_menu(_chat_of(cb)))


# ── Экран выбора игр и выход ───────────────────────────────────────────────
@router.callback_query(F.data == "pet:games")
async def games_screen(cb: CallbackQuery, session: AsyncSession) -> None:
    """🎮 Игровая — экран выбора мини-игры (тот же страж и тот же стиль отказа)."""
    entry = await _game_entry_guard(cb, session)
    if entry is None:
        return
    _, pet = entry
    await _games_screen_render(cb, pet)


@router.callback_query(F.data == "game:exit")
async def game_exit(cb: CallbackQuery, session: AsyncSession) -> None:
    """⬅️ Выйти из мини-игры → экран меню игр."""
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await _deny(cb, "🥚 Сначала заведи питомца — /start")
        return
    set_pet_page(_chat_of(cb) or 0, 2)
    await _games_screen_render(cb, pet)


# ── 🔢 Угадай число ────────────────────────────────────────────────────────
# Секрет партии кладётся в КАЖДУЮ кнопку-вариант: «guess:<число>:<секрет>».
# Контекст хода не зависит ни от FSM, ни от клавиатуры сообщения, ни от
# Redis — он прямо под пальцем. Тап по кнопке СТАРОЙ партии отличим по
# отсутствию второй части / невалидным данным → возврат в меню, не тишина.

def _guess_secret_range(pet) -> tuple[int, tuple[int, int]]:
    svc = TamagotchiService(None)
    return svc.guess_range(pet)


def _guess_kb(lo: int, hi: int, secret: int, chat_id: int | None) -> InlineKeyboardMarkup:
    mid = (lo + hi) // 2
    uniq = sorted({lo, mid, hi})   # при узком диапазоне lo==mid — дубликаты недопустимы
    rows = [[(str(n), f"guess:{n}:{secret}") for n in uniq],
            [("🔄 Новая игра", f"guess:new:{secret}")]]
    return inline_back_kb("games", chat_id=chat_id, extra_rows=rows)


# Снимок активной партии угадайки: chat_id -> секрет. Нужен ТОЛЬКО для
# текстовых ходов (callback-ходы самодостаточны и его не читают).
# LRU с лимитом: обычный dict оставлял бы запись каждого чата навсегда.
from app.utils.chat_ctx import BoundedChatCtx
_CHAT_GUESS_SECRET = BoundedChatCtx(maxsize=4096)


async def _guess_start(cb: CallbackQuery, session: AsyncSession) -> None:
    """Общий старт/рестарт партии (кнопка игры и «🔄 Новая игра»)."""
    entry = await _game_entry_guard(cb, session)
    if entry is None:
        return
    _, pet = entry
    secret, (lo, hi) = _guess_secret_range(pet)
    await safe_edit_or_answer(
        cb.message,
        f"🔢 {pet.name} загадал число от 1 до 20. Друзья шепчут, что оно в диапазоне "
        f"<b>{lo}…{hi}</b> (чем умнее питомец, тем точнее подсказка!).\n\n"
        "Нажми кнопку-вариант или напиши своё число сообщением:",
        reply_markup=_guess_kb(lo, hi, secret, _chat_of(cb)))
    _CHAT_GUESS_SECRET.set(_chat_of(cb) or 0, secret)
    await cb.answer()


@router.callback_query(F.data == "game:guess")
async def start_guess(cb: CallbackQuery, session: AsyncSession) -> None:
    await _guess_start(cb, session)


@router.callback_query(F.data.startswith("guess:new:"))
async def guess_new(cb: CallbackQuery, session: AsyncSession) -> None:
    """🔄 Новая партия угадайки без возврата в меню игр."""
    await _guess_start(cb, session)


@router.callback_query(F.data.startswith("guess:"))
async def do_guess_cb(cb: CallbackQuery, session: AsyncSession) -> None:
    parts = (cb.data or "").split(":")
    try:
        guess = int(parts[1])
        secret = int(parts[2])
    except (ValueError, IndexError):
        await _stale_game_screen(cb, session, "🔢 Угадай число")
        return
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await _deny(cb, "🥚 Сначала заведи питомца — /start")
        return
    won = guess == secret
    hint = "" if won else f" Это было число <b>{secret}</b>. Попробуй ещё!"
    line = (f"🔢 Ты выбрал <b>{guess}</b>.\n"
            f"{'🎯 Точно!' if won else '❌ Мимо.'}{hint}\n\n")
    credited = await _finish_game(cb, session, pet, won, kind="guess",
                                  meta={"guess": guess}, line=line)
    if credited:
        await cb.answer()


@router.message(F.text & F.text.strip().isdigit())
async def do_guess_msg(message: Message, session: AsyncSession) -> None:
    """Числовое сообщение = ход в угадайку, если в чате открыта партия.

    Активная партия определяется по лёгкому снимку секретов чатов
    (_CHAT_GUESS_SECRET) — без FSM. Числа вне 1–20 считаются обычным
    сообщением (защита от ложных срабатываний на цифровой ввод).
    """
    txt = message.text.strip()
    guess = int(txt)
    if not 1 <= guess <= 20:
        return
    secret = _CHAT_GUESS_SECRET.get(message.chat.id)
    if secret is None:
        return   # активной партии нет — это просто сообщение, не ход
    pet = await _get_pet(session, message.from_user.id)
    if pet is None:
        return
    won = guess == secret
    svc = TamagotchiService(session)
    result = await svc.play(pet, won)
    if _outcome_is_denial(result):
        await message.answer(result, parse_mode="HTML")
        await session.commit()
        return
    hint = "" if won else f" Это было число <b>{secret}</b>."
    await PetRepository(session).log_action(
        pet.id, "game", value=int(won), meta={"kind": "guess", "guess": guess})
    if won:
        await bump_games_won(session, message.from_user.id)
    await message.answer(f"{result}{hint}\n\n{await svc.render_async(pet)}",
                         reply_markup=games_menu(message.chat.id),
                         parse_mode="HTML")
    await session.commit()


# ── ✂️ Камень-ножницы-бумага ───────────────────────────────────────────────
@router.callback_query(F.data == "game:rps")
async def start_rps(cb: CallbackQuery, session: AsyncSession) -> None:
    entry = await _game_entry_guard(cb, session)
    if entry is None:
        return
    _, pet = entry
    # Ход питомца ЖЕРЕБУЕТСЯ ЗАРАНЕЕ и прячется в callback_data кнопок
    # выбора (например «rps:rock:p»): игрок выбирает вслепую («синхронное
    # раскрытие»), а не получает ответ постфактум. Контекст живёт прямо в
    # кнопке — переживает перезапуск бота и потерю любых хранилищ.
    pet_hand = random.choice(list(RPS_EMOJI))
    await safe_edit_or_answer(
        cb.message,
        "✂️ <b>Камень-ножницы-бумага!</b>\n\n"
        f"{pet.name} уже тайно выбрал свой ход 🤫 (честный рандом).\n"
        "Выбирай свой — откроемся одновременно.\n\n"
        "Правила: 🪨 бьёт ✂️ · ✂️ режут 📄 · 📄 накрывает 🪨",
        reply_markup=_rps_kb(pet_hand, _chat_of(cb)))
    await cb.answer()


def _rps_kb(pet_hand: str, chat_id: int | None) -> InlineKeyboardMarkup:
    """Кнопки хода, в callback_data которых спрятан тайный ход питомца:
    «rps:<мой ход>:<ход питомца>». Ход полностью самодостаточен."""
    rows = [[(label, f"rps:{hand}:{pet_hand}") for hand, label in RPS_EMOJI.items()]]
    return inline_back_kb("games", chat_id=chat_id, extra_rows=rows)


@router.callback_query(F.data.startswith("rps:"))
async def play_rps(cb: CallbackQuery, session: AsyncSession) -> None:
    parts = (cb.data or "").split(":")
    mine = parts[1] if len(parts) > 1 else ""
    theirs = parts[2] if len(parts) > 2 else ""
    if mine not in RPS_EMOJI:
        await cb.answer()
        return
    if theirs not in RPS_EMOJI:
        # Тап по кнопке старой партии (питомец уже раскрылся) — в меню игр.
        await _stale_game_screen(cb, session, "✂️ Камень-ножницы-бумага")
        return
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await _deny(cb, "🥚 Сначала заведи питомца — /start")
        return
    # Единый источник истины по правилам КНБ (см. RPS_BEATS): побеждает тот,
    # чей ход бьёт ход соперника. Исход полностью определяется самими
    # ходами: ✂️ режут 📄, значит выиграл показавший ✂️.
    outcome = rps_outcome(mine, theirs)
    won = outcome == "win"
    draw = outcome == "draw"
    if draw:
        line = (f"Ты: {RPS_EMOJI[mine]} · {pet.name}: {RPS_EMOJI[theirs]} — "
                "🤝 ничья, одинаковые ходы.\n\n")
    elif won:
        line = (f"Ты: {RPS_EMOJI[mine]} · {pet.name}: {RPS_EMOJI[theirs]} — "
                f"✅ твой ход бьёт: {RPS_EMOJI[mine]} побеждает {RPS_EMOJI[theirs]}.\n\n")
    else:
        line = (f"Ты: {RPS_EMOJI[mine]} · {pet.name}: {RPS_EMOJI[theirs]} — "
                f"❌ ход питомца бьёт: {RPS_EMOJI[theirs]} побеждает {RPS_EMOJI[mine]}.\n\n")
    credited = await _finish_game(cb, session, pet, won, draw, kind="rps",
                                  meta={"mine": mine, "theirs": theirs}, line=line)
    if credited:
        await cb.answer()


# ── 🃏 Двадцать одно ───────────────────────────────────────────────────────
BJ_DECK = [(r, s) for r in range(2, 11) for s in ("♠", "♥", "♦", "♣")]
BJ_LABELS = {2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9",
             10: "10", 11: "Т"}


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


def _card_str(cards: list[tuple[int, str]]) -> str:
    return " ".join(BJ_LABELS.get(r, "?") + s for r, s in _bj_norm(cards)) or "—"


def _bj_render(cards: list[tuple[int, str]], hidden: bool = False) -> str:
    cards = _bj_norm(cards)
    if hidden and cards:
        return f"{_card_str([cards[0]])} + 🂠"
    return _card_str(cards)


# ── Хранилище партий «Двадцать одно» ──────────────────────────────────────
# Почему нельзя было прятать колоду в callback_data: Telegram жёстко
# ограничивает callback_data 64 байтами. Полный снимок колоды весит
# ~160 байт, поэтому edit_message_text падал сBadRequest («Button
# callback_data is too long») — игра стартовала, а первый же тап
# «Ещё карту» / «Хватит» ничего не делал. Это и была причина, по которой
# КНБ и угадайка работали (их токены ≤ 17 байт), а блэкджек нет.
#
# Решение: состояние партии хранится в памяти процесса под коротким
# подписанным id (8 hex + подпись). В кнопке теперь всегда ≤ 25 байт.
# Партия живёт 30 минут; после рестарта бота старый экран корректно
# отправляет в меню («партия завершена»), вместо мёртвых кнопок.
_BJ_GAMES: TTLCache = TTLCache(maxsize=4096, ttl=30 * 60)
_BJ_SECRET = os.environ.get("BJ_STATE_SECRET") or secrets.token_hex(16)


def _bj_sign(mid: str) -> str:
    return f"{zlib.crc32((mid + _BJ_SECRET).encode()) & 0xFFFFFFFF:x}"[:6]


def _bj_state_b64(deck, player, dealer, stay: int) -> str:
    """Сохраняет снимок партии, возвращает КОРОТКИЙ id для callback_data."""
    while True:
        mid = secrets.token_hex(4)
        if mid not in _BJ_GAMES:
            break
    _BJ_GAMES[mid] = ([list(c) for c in deck], [list(c) for c in player],
                      [list(c) for c in dealer], int(stay))
    return f"{mid}.{_bj_sign(mid)}"


def _bj_load_state(data: str):
    """('ok', deck, player, dealer, stay) | ('stale',) | ('bad',).

    Формат кнопки: «bj:<действие>:<id>.<подпись>». Парсим СПРАВА НАЛЕВО
    (rsplit), чтобы действие не съедалось токеном. Несовпадение подписи
    = «bad» (ручное редактирование кнопки); отсутствие/истечение партии
    = «stale» (рестарт бота, тап по старому экрану) — оба случая дают
    явный ответ и возврат в меню, никогда тишину.
    """
    parts = data.rsplit(":", 1)
    token = parts[1] if len(parts) == 2 else ""
    mid, _, sig = token.rpartition(".")
    if not mid or len(token) > 32:
        return ("stale",)
    if sig != _bj_sign(mid):
        return ("bad",)
    state = _BJ_GAMES.get(mid)
    if state is None:
        return ("stale",)
    deck, player, dealer, stay = state
    return ("ok", [tuple(c) for c in deck], [tuple(c) for c in player],
            [tuple(c) for c in dealer], int(stay))


def _bj_forget(data: str) -> None:
    """Удаляет партию по завершении раунда (одноразовость id)."""
    parts = data.rsplit(":", 1)
    token = parts[1] if len(parts) == 2 else ""
    mid, _, sig = token.rpartition(".")
    if mid and sig == _bj_sign(mid):
        _BJ_GAMES.pop(mid, None)


def _bj_kb(state_token: str, chat_id: int | None) -> InlineKeyboardMarkup:
    rows = [[("➕ Ещё карту", f"bj:hit:{state_token}"),
             ("✋ Хватит", f"bj:stand:{state_token}")]]
    return inline_back_kb("games", chat_id=chat_id, extra_rows=rows)


@router.callback_query(F.data == "game:blackjack")
async def start_blackjack(cb: CallbackQuery, session: AsyncSession) -> None:
    entry = await _game_entry_guard(cb, session)
    if entry is None:
        return
    _, pet = entry
    rng = random.Random()
    deck = BJ_DECK[:]
    rng.shuffle(deck)
    dealer_stay = 17 + min(3, pet.intellect // 6)
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    token = _bj_state_b64(deck, player, dealer, dealer_stay)
    await safe_edit_or_answer(
        cb.message,
        f"🃏 <b>Двадцать одно!</b> {pet.name} — дилер.\n\n"
        f"Твои карты: <b>{_bj_render(player)}</b> ({_bj_value(player)})\n"
        f"Карты дилера: <b>{_bj_render(dealer, hidden=True)}</b>\n\n"
        "«Ещё» — взять карту, «Хватит» — остановиться. Больше 21 — перебор!",
        reply_markup=_bj_kb(token, _chat_of(cb)))
    await cb.answer()


async def _bj_finish(cb: CallbackQuery, session: AsyncSession, pet,
                     player: list, dealer: list) -> bool:
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
    line = (f"Твои: <b>{_bj_render(player)}</b> ({pv}) · "
            f"{pet.name}: <b>{_bj_render(dealer)}</b> ({dv})\n{outcome}\n\n")
    return await _finish_game(cb, session, pet, won, draw=(pv == dv),
                              kind="blackjack", meta={"player": pv, "dealer": dv},
                              line=line)


@router.callback_query(F.data.startswith("bj:hit:"))
async def bj_hit(cb: CallbackQuery, session: AsyncSession) -> None:
    loaded = _bj_load_state(cb.data or "")
    if loaded[0] != "ok":
        await _stale_game_screen(cb, session, "🃏 Двадцать одно")
        return
    _, deck, player, dealer, stay = loaded
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await _deny(cb, "🥚 Сначала заведи питомца — /start")
        return
    if not deck:
        await _deny(cb, "🃏 Колода кончилась — начни игру заново")
        return
    player.append(deck.pop())
    pv = _bj_value(player)
    if pv >= 21:
        finished = await _bj_finish(cb, session, pet, player, dealer)
        _bj_forget(cb.data or "")   # раунд сыгран — id больше не живёт
        if finished:
            await cb.answer()
        return
    token = _bj_state_b64(deck, player, dealer, stay)
    _bj_forget(cb.data or "")       # старый снимок заменён новым
    await safe_edit_or_answer(
        cb.message,
        f"🃏 Твои карты: <b>{_bj_render(player)}</b> ({pv})\n"
        f"Карты дилера: <b>{_bj_render(dealer, hidden=True)}</b>\n\n"
        "Ещё или хватит?",
        reply_markup=_bj_kb(token, _chat_of(cb)))
    await cb.answer()


@router.callback_query(F.data.startswith("bj:stand:"))
async def bj_stand(cb: CallbackQuery, session: AsyncSession) -> None:
    loaded = _bj_load_state(cb.data or "")
    if loaded[0] != "ok":
        await _stale_game_screen(cb, session, "🃏 Двадцать одно")
        return
    _, deck, player, dealer, stay = loaded
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await _deny(cb, "🥚 Сначала заведи питомца — /start")
        return
    while _bj_value(dealer) < stay and deck:
        dealer.append(deck.pop())
    finished = await _bj_finish(cb, session, pet, player, dealer)
    _bj_forget(cb.data or "")   # раунд сыгран — id больше не живёт
    if finished:
        await cb.answer()


async def bump_games_won(session: AsyncSession, tg_id: int) -> None:
    users = UserRepository(session)
    new_val = await users.bump_stat(tg_id, "games_won", 1)
    await AchievementService(session).check(tg_id, {"games_won": new_val})
