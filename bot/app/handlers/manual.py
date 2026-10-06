"""📖 Гид по уходу за питомцем + ⚙️ админский экран глобального баланса.

manual:* — публичная справка, тексты генерируются из SPECIES_DATA и
           balance.snapshot() (всегда актуальные цифры).
bal:*    — настройки множителей баланса; видна только ADMIN_IDS, меняет
           значения в памяти процесса (после рестарта — возврат к .env).
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.keyboards.inline import with_nav
from app.services import balance, pet_manual
from app.utils.safe_edit import safe_edit_or_answer

router = Router(name="manual")


# ── 📖 Гид по уходу ────────────────────────────────────────────────────────

def _manual_home_kb(chat_id: int | None = None) -> InlineKeyboardMarkup:
    # Ровная сетка 2×N: кнопки всегда по ДВЕ в ряд. Виды парами
    # (котёнок/щенок, лисёнок/шиншилла, совёнок/дракончик), затем служебные
    # экраны той же сеткой («📊 Показатели» + «🎮 Как устроены игры» одной
    # строкой). Навигация with_nav() добавляется ПОСЛЕ adjust(2): append_nav
    # вызывает .row(), который фиксирует уже набранные кнопки как готовый
    # ряд — если вызвать adjust в конце, одиночные .row() между кнопками
    # оставят ряды разной ширины и сетка «съедет».
    b = InlineKeyboardBuilder()
    for code, sp in pet_manual.SPECIES_DATA.items():
        b.button(text=f"{sp['emoji']} {sp['title']}",
                 callback_data=f"manual:species:{code}")
    b.button(text="📊 Показатели: как качать", callback_data="manual:stats")
    b.button(text="🎮 Как устроены игры", callback_data="manual:games")
    b.adjust(2)
    with_nav(b, "manual", chat_id)
    return b.as_markup()


def _manual_sub_kb(chat_id: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К списку тем", callback_data="manual:home")
    with_nav(b, "manual", chat_id, current_cb="manual:home")
    return b.as_markup()


@router.callback_query(F.data == "manual:home")
async def cb_manual_home(cb: CallbackQuery, session: AsyncSession) -> None:
    await safe_edit_or_answer(
        cb.message,
        pet_manual.home_text() + "\n".join(
            f"{sp['emoji']} <b>{sp['title']}</b>"
            for sp in pet_manual.SPECIES_DATA.values()),
        reply_markup=_manual_home_kb(cb.message.chat.id if cb.message else None))
    await cb.answer()


@router.callback_query(F.data.startswith("manual:species:"))
async def cb_manual_species(cb: CallbackQuery, session: AsyncSession) -> None:
    code = cb.data.split(":", 2)[2]
    text = pet_manual.species_text(code)
    if not text:
        await cb.answer("Неизвестный вид", show_alert=True)
        return
    await safe_edit_or_answer(cb.message, text,
                              reply_markup=_manual_sub_kb(
                                  cb.message.chat.id if cb.message else None))
    await cb.answer()


@router.callback_query(F.data == "manual:stats")
async def cb_manual_stats(cb: CallbackQuery, session: AsyncSession) -> None:
    await safe_edit_or_answer(cb.message, pet_manual.stats_guide_text(),
                              reply_markup=_manual_sub_kb(
                                  cb.message.chat.id if cb.message else None))
    await cb.answer()


@router.callback_query(F.data == "manual:games")
async def cb_manual_games(cb: CallbackQuery, session: AsyncSession) -> None:
    await safe_edit_or_answer(cb.message, pet_manual.games_guide_text(),
                              reply_markup=_manual_sub_kb(
                                  cb.message.chat.id if cb.message else None))
    await cb.answer()


@router.message(Command("manual", "guide", "help_pet"), F.chat.type == "private")
async def cmd_manual(message: Message, session: AsyncSession) -> None:
    await message.answer(pet_manual.home_text() + "\n".join(
        f"{sp['emoji']} <b>{sp['title']}</b>"
        for sp in pet_manual.SPECIES_DATA.values()),
        reply_markup=_manual_home_kb(message.chat.id))


async def route_manual_callback(cb: CallbackQuery, session: AsyncSession) -> None:
    """Программный диспетчер всех «manual:*» — один обработчик на все случаи.

    Нужен как «мост» для роутеров, зарегистрированных без manual.router
    (см. events.py): кнопки гида не должны молчать, если порядок регистрации
    роутеров где-то сбился. Делегирует тем же функциям-хендлерам, что и
    маршруты выше, поэтому поведение идентично.
    """
    data = cb.data or ""
    if data.startswith("manual:species:"):
        await cb_manual_species(cb, session)
    elif data == "manual:stats":
        await cb_manual_stats(cb, session)
    elif data == "manual:games":
        await cb_manual_games(cb, session)
    else:  # manual:home и любые будущие подэкраны корня гида
        await cb_manual_home(cb, session)


# ── ⚙️ Баланс (только администраторы) ─────────────────────────────────────

def _is_admin(user_id: int) -> bool:
    try:
        return user_id in set(get_settings().admin_ids or [])
    except Exception:
        return False


def _fmt(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s or "0"


def _balance_text() -> str:
    env_src = balance.env_overridden_keys()
    L = ["⚙️ <b>Глобальный баланс питомца</b>", "",
         "Множители действуют на ВСЕХ питомцев сразу (единая экономика — без",
         "пользовательских «ускорений», чтобы никто не получал преимущества).",
         "Шаг кнопок ×1.25 / ÷1.25. Значения живут до перезапуска бота;",
         "фундамент задаётся переменными BALANCE_* в .env.", "",
         "<i>Помечено 🔒 — сейчас взято из .env (кнопки переопределяют на ходу).</i>", ""]
    for key, (_env, _base, label) in balance.BALANCE_KEYS.items():
        lock = " 🔒" if key in env_src else ""
        L.append(f"{label}: <b>{_fmt(balance.get_mult(key))}</b>{lock}")
    return "\n".join(L)


def _balance_kb(chat_id: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, (_env, _base, label) in balance.BALANCE_KEYS.items():
        short = label.split(" ", 1)[0]          # эмодзи-маркер строки
        name = label.split(" ", 1)[1][:22]
        b.button(text=f"➕ {short}＋", callback_data=f"bal:up:{key}")
        b.button(text=f"➖ {short}－", callback_data=f"bal:down:{key}")
        b.row()
        b.button(text=f"↩️ Сброс: {name}", callback_data=f"bal:reset:{key}")
        b.row()
    b.button(text="♻️ Сбросить всё к .env", callback_data="bal:reset_all")
    with_nav(b, "balance", chat_id)
    return b.as_markup()


@router.callback_query(F.data == "bal:home")
async def cb_balance_home(cb: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администраторов", show_alert=True)
        return
    await safe_edit_or_answer(cb.message, _balance_text(),
                              reply_markup=_balance_kb(
                                  cb.message.chat.id if cb.message else None))
    await cb.answer()


@router.message(Command("balance"), F.chat.type == "private")
async def cmd_balance(message: Message, session: AsyncSession) -> None:
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ Команда доступна только администраторам.")
        return
    await message.answer(_balance_text(), reply_markup=_balance_kb(message.chat.id))


@router.callback_query(F.data.startswith("bal:up:") | F.data.startswith("bal:down:"))
async def cb_balance_bump(cb: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администраторов", show_alert=True)
        return
    up = cb.data.startswith("bal:up:")
    key = cb.data.split(":", 2)[2]
    new = balance.bump(key, up=up)
    if new is None:
        await cb.answer("Неизвестный ключ", show_alert=True)
        return
    await safe_edit_or_answer(cb.message, _balance_text(),
                              reply_markup=_balance_kb(
                                  cb.message.chat.id if cb.message else None))
    await cb.answer(f"{key} → {_fmt(new)}")


@router.callback_query(F.data.startswith("bal:reset:"))
async def cb_balance_reset_one(cb: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администраторов", show_alert=True)
        return
    key = cb.data.split(":", 2)[2]
    balance.reset(key)
    await safe_edit_or_answer(cb.message, _balance_text(),
                              reply_markup=_balance_kb(
                                  cb.message.chat.id if cb.message else None))
    await cb.answer(f"Сброшено к значению по умолчанию: {key}")


@router.callback_query(F.data == "bal:reset_all")
async def cb_balance_reset_all(cb: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администраторов", show_alert=True)
        return
    balance.reset()
    await safe_edit_or_answer(cb.message, _balance_text(),
                              reply_markup=_balance_kb(
                                  cb.message.chat.id if cb.message else None))
    await cb.answer("Все множители сброшены к .env/базовым")
