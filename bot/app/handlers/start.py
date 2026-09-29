"""Хендлеры /start, онбординг (имя питомца), главное меню."""
from __future__ import annotations

import contextlib

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
import html as _html
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pet, PetSpecies
from app.db.repositories import PetRepository, UserRepository
from app.keyboards.inline import (
    MENU_PAGES, main_menu, onboard_done, species_picker,
    start_pet_name_suggestions, welcome_start_button,
)
from app.services.achievements import AchievementService
from app.services.tamagotchi import SPECIES_DATA
from app.utils.formatting import progress_bar, xp_needed_for_level
from app.utils.safe_edit import safe_edit_or_answer
from app.config import get_settings
from loguru import logger
from app.middlewares.gate import is_channel_subscribed, reset_subscribe_cache, subscribe_kb

router = Router(name="start")

PET_NAME_SUGGESTIONS = ["Барсик", "Мурка", "Персик", "Кузя", "Соня", "Имя своё…"]

class Onboarding(StatesGroup):
    choosing_pet_species = State()
    choosing_pet_name = State()

def species_picker_text() -> str:
    """Описание видов для экрана выбора (используется в онбординге).

    Только по-русски: занятия и бонусы читаются из единого справочника
    ACTION_LABELS / BONUS_LABELS (app.services.tamagotchi), служебные
    английские коды («play», «coin_mult») пользователю не показываем.
    """
    from app.services.tamagotchi import species_bonuses_text, species_likes_text

    lines = []
    for code, sp in SPECIES_DATA.items():
        st = sp["start"]
        likes, dislikes = species_likes_text(sp)
        bonuses = species_bonuses_text(sp)
        lines.append(
            f"{sp['emoji']} <b>{sp['title']}</b> — {sp['desc']}\n"
            f"   💪 сила {st['strength']} · 🏃 ловкость {st['agility']} · "
            f"🧠 интеллект {st['intellect']}\n"
            f"   ❤️ любит: {likes}\n"
            f"   💔 не любит: {dislikes}\n"
            f"   🎁 бонусы: {bonuses}"
        )
    return "\n\n".join(lines)

WELCOME_DM = (
    "👋 Привет, <b>{name}</b>!\n\n"
    "Я — бот-компаньон канала (v1.5). Вот что я умею:\n"
    "• 🐾 Виртуальный питомец-тамагочи — корми, мой, играй, тренируй, гуляй;\n"
    "   он растёт (яйцо → легенда), болеет и лечится вместе с тобой;\n"
    "• 🌦️ Живая погода Петропавловска-Камчатского (OpenWeather): солнце бодрит\n"
    "   питомца и удваивает находки на прогулке, дождь и мороз — грустят и могут\n"
    "   простудить; перед прогулкой бот покажет прогноз на ближайшие 3 часа;\n"
    "• 📊 Активность — за сообщения в чате (текст, фото, голосовые, кружки,\n"
    "   стикеры) и реакции капают XP и 🪙 монеты; ежедневный стрик множит награды;\n"
    "• 🏆 20+ достижений и уровни — прогресс считается автоматически, в том\n"
    "   числе за действия с питомцем (кормления, прогулки);\n"
    "• ⚔️ Арена — еженедельные дуэли питомцев за призы;\n"
    "• 🏅 Топы недели — покажи, кто тут главный болтун;\n"
    "• 🛒 Магазин и 🎒 инвентарь — еда, энергетики, лекарства и щиты от простуды\n"
    "   (внутри «🐾 Питомец» → «🎒 Вещи», страницы листаются ◀️ ▶️);\n"
    "• 🖼 PNG-карточка профиля со всеми статами.\n\n"
    "Полная справка — /help, погода и её плюсы/минусы — /weather.\n"
    "Правила простые: общайся в чате, не флуди, ставь реакции — это тоже считается.\n"
    "{channel_line}\n"
    "Нажми «Начать», чтобы завести питомца!"
)

def _channel_line() -> str:
    ch = get_settings().channel_username
    return f"📢 Наш канал: t.me/{ch}\n" if ch else ""

def invite_link_for(tg_id: int) -> str:
    """Реферальная ссылка ведёт на КАНАЛ (не в группу): t.me/<channel>?start=invite_<id>.

    Если CHANNEL_USERNAME не задан — честно возвращаем пустую строку, чтобы нигде
    не появилась битая/групповая ссылка.
    """
    ch = get_settings().channel_username
    if not ch:
        return ""
    return f"https://t.me/{ch}?start=invite_{tg_id}"

def _main_menu_text(user, page: int = 0) -> str:
    need = xp_needed_for_level(user.level)
    bar = progress_bar(user.xp, need)
    ch = get_settings().channel_username
    title, _actions = MENU_PAGES[page % len(MENU_PAGES)]
    lines = [
        f"🏠 <b>Главное меню · {title}</b>\n",
        f"👤 {_html.escape(user.first_name or '')}, уровень {user.level} · {bar} {user.xp}/{need} XP",
        f"🪙 Монеты: {user.coins} · 🔥 Серия: {user.streak_days} дн.",
        "",
        "📌 Что делать:",
        "• 🐾 Зайди к питомцу — покорми его (голод никуда не делся!)",
        "• 🌦️ Загляни в /weather — от живой погоды Камчатки зависят прогулки:",
        "   солнце = +находки и 😊 Счастье, дождь/мороз = риск простуды",
        "• 💬 Напиши в чат — засчитывается текст, фото, голос, кружок, стикер",
        "• ❤️ Ставь реакции — за них тоже капает XP",
        "• 🛒 Копи монеты — магазин (в «🐾 Питомец» → «🎒 Вещи») и мерч уже ждут",
        "• ⚔️ Попробуй Арену — еженедельные дуэли питомцев за призы",
    ]
    if ch:
        lines += ["", f"📢 Новости канала: t.me/{ch}"]
    return "\n".join(lines)

private_only = F.chat.type == "private"

@router.message(CommandStart(), private_only)
async def cmd_start(message: Message, state: FSMContext, session: AsyncSession,
                    command: CommandObject | None = None) -> None:
    """Основной вход: регистрация + онбординг + информативное главное меню.

    Deep-link `?start=invite_<tg_id>`: новичок регистрируется, связка
    «пригласивший → новичок» сохраняется в БД (User.referrer_id). Награда
    пригласившему выдаётся ОДНОКРАТНО в ActivityService._credit_referral —
    когда новичок проявит первую засчитанную активность в чате (защита от
    накрутки пустыми регистрациями). Здесь дополнительно показываем новичку,
    КТО его пригласил, — так связка «ссылка на канал → /start в боте»
    работает end-to-end.
    """
    users = UserRepository(session)
    user = await users.get_or_create(
        tg_id=message.from_user.id,
        first_name=message.from_user.first_name or "",
        username=message.from_user.username,
    )
    from app.handlers.access import register_member
    await register_member(
        user.tg_id, contacted=True,
        first_name=user.first_name or "", username=user.username)
    payload = (command.args or "") if command else ""
    if payload.startswith("invite_"):
        try:
            inviter_id = int(payload.split("invite_", 1)[1].split()[0])
        except (ValueError, IndexError):
            inviter_id = None
        if inviter_id and inviter_id != user.tg_id:
            inviter = await users.get(inviter_id)
            who = _html.escape(inviter.first_name if inviter and inviter.first_name else "друг")
            await users.set_referrer(user.tg_id, inviter_id)
            await message.answer(
                f"🤝 Тебя пригласил <b>{who}</b>! После онбординга он получит "
                f"+{get_settings().invite_reward_coins} 🪙, как только ты напишешь первое сообщение в чате.",
                parse_mode="HTML",
            )
    if not user.onboarded:
        await message.answer(
            WELCOME_DM.format(name=_html.escape(user.first_name or "друг"),
                                channel_line=_channel_line()),
            reply_markup=welcome_start_button(),
        )
        return
    link = invite_link_for(user.tg_id)
    reward = get_settings().invite_reward_coins
    text = _main_menu_text(user)
    await message.answer(text, reply_markup=main_menu(link=link, reward=reward),
                       parse_mode="HTML")

@router.callback_query(F.data == "gate:check")
async def cb_gate_check(cb: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    """«Я подписался — проверить»: после успешной проверки сразу в меню.

    v2.0.6 (боевой лог 10:45): раньше при неудаче здесь висело «Подписка не
    найдена 😔» без всякого диагноза — человек состоял в канале, но источник
    молчал, и причина терялась. Теперь перед ответом прогоняется полная
    цепочка гейта с подробным логом (gate: DENY … | Bot API: … | registry: …),
    а пользователю показывается понятная подсказка; админ получает сырой
    диагноз отдельным сообщением.
    """
    reset_subscribe_cache(cb.from_user.id)
    if not await is_channel_subscribed(bot, cb.from_user.id):
        uid = cb.from_user.id
        diag = []
        with contextlib.suppress(Exception):
            from app.middlewares.gate import _registry_row_state, required_chats
            st = get_settings()
            api_notes = []
            for cid, uname in required_chats():
                target = f"@{uname}" if uname else cid
                try:
                    m = await bot.get_chat_member(target, uid)
                    api_notes.append(f"{target}: {getattr(m, 'status', '?')}")
                except Exception as exc:
                    api_notes.append(f"{target}: {type(exc).__name__}")
            diag.append("Bot API: " + "; ".join(api_notes))
            diag.append(f"Реестр: {await _registry_row_state(uid)}")
        logger.info("gate:check failed for {}: {}", uid, " | ".join(diag))
        await cb.answer(
            "Подписка не найдена 😔\nЕсли ты точно в канале/группе — напиши "
            "/start ещё раз через пару минут: бот перепроверит по всем "
            "источникам.", show_alert=True)
        return
    users = UserRepository(session)
    user = await users.get_or_create(cb.from_user.id, cb.from_user.first_name or "",
                                     cb.from_user.username)
    await safe_edit_or_answer(
        cb.message, _main_menu_text(user),
        reply_markup=main_menu(link=invite_link_for(user.tg_id),
                               reward=get_settings().invite_reward_coins))
    await cb.answer("Ура, добро пожаловать! 🎉")

@router.callback_query(F.data == "onb:start")
async def cb_onboard_start(cb: CallbackQuery, state: FSMContext,
                           session: AsyncSession) -> None:
    users = UserRepository(session)
    user = await users.get_or_create(cb.from_user.id, cb.from_user.first_name or "",
                                     cb.from_user.username)
    if user.onboarded:
        await safe_edit_or_answer(cb.message, "Ты уже с нами! 🎉", reply_markup=main_menu())
        await cb.answer()
        return
    await state.set_state(Onboarding.choosing_pet_species)
    await safe_edit_or_answer(cb.message, 
        "🐣 Шаг 1 из 3. Выбери питомца — у каждого свой характер и бонусы:\n\n"
        + species_picker_text(),
        reply_markup=species_picker(),
    )
    await cb.answer()

@router.callback_query(F.data == "onb:skip")
async def cb_onboard_skip(cb: CallbackQuery, state: FSMContext,
                          session: AsyncSession) -> None:
    """Онбординг без питомца : статистика/топы работают и так.

    Питомец — опция: пользователь может завести его позже кнопкой
    «🥚 Усыновить» (pet:adopt) или из пикера вида. Ачивку first_steps не
    выдаём — она про рождение питомца.
    """
    users = UserRepository(session)
    user = await users.get_or_create(cb.from_user.id, cb.from_user.first_name or "",
                                     cb.from_user.username)
    if user.onboarded:
        await safe_edit_or_answer(cb.message, "Ты уже с нами! 🎉", reply_markup=main_menu())
        await cb.answer()
        return
    user.onboarded = True
    await state.clear()
    await session.commit()
    await safe_edit_or_answer(
        cb.message,
        "🤝 Понял — наблюдаем со стороны!\n\n"
        "• 🏅 Топы и 📊 Статы считаются автоматически по активности в чате;\n"
        "• 🐾 питомца можно завести в любой момент — кнопка ниже;\n"
        "• 🎁 монеты капают за сообщения, их можно копить даже без игры.\n\n"
        "Зайди в группу и напиши что-нибудь — это засчитается как активность 👇",
        reply_markup=_no_pet_menu_kb(),
    )
    await cb.answer()

def _no_pet_menu_kb():
    from app.keyboards.inline import adopt_cta_kb
    return adopt_cta_kb()

@router.callback_query(Onboarding.choosing_pet_species, F.data.startswith("onb:species:"))
async def cb_pick_species(cb: CallbackQuery, state: FSMContext) -> None:
    code = cb.data.split(":")[2]
    if code not in SPECIES_DATA:
        await cb.answer("Такого питомца нет", show_alert=True)
        return
    await state.update_data(species=code)
    await state.set_state(Onboarding.choosing_pet_name)
    await safe_edit_or_answer(cb.message, 
        f"{SPECIES_DATA[code]['emoji']} Отличный выбор — {SPECIES_DATA[code]['title']}!\n\n"
        "Шаг 2 из 3. Выбери имя питомцу (или напиши своё сообщением):",
        reply_markup=start_pet_name_suggestions(PET_NAME_SUGGESTIONS),
    )
    await cb.answer()

@router.callback_query(Onboarding.choosing_pet_name, F.data.startswith("onb:name:"))
async def cb_pick_name(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    name = cb.data.split(":", 2)[2]
    if name == "Имя своё…":
        await safe_edit_or_answer(cb.message, "✍️ Напиши своё имя питомца сообщением:")
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
        f"🎉 У тебя появился питомец <b>{_html.escape(name)}</b> — "
        f"{SPECIES_DATA[species_code]['emoji']} "
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
    await safe_edit_or_answer(cb.message, 
        f"🎉 У тебя появился питомец <b>{_html.escape(name)}</b> — "
        f"{sp['emoji']} {sp['title']}!\n\n"
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
async def cb_main_menu(cb: CallbackQuery, session: AsyncSession,
                       state: FSMContext) -> None:
    if await state.get_state() is not None:
        await state.clear()
    await _render_main_menu(cb, session, page=0)

@router.callback_query(F.data.startswith("menu:page:"))
async def cb_main_menu_page(cb: CallbackQuery, session: AsyncSession,
                            state: FSMContext) -> None:
    """◀️/▶️ главного меню : страницы «Игра» и «Профиль»."""
    if await state.get_state() is not None:
        await state.clear()
    try:
        page = int(cb.data.split(":")[-1])
    except ValueError:
        page = 0
    await _render_main_menu(cb, session, page=page)

@router.callback_query(F.data == "menu:noop")
async def cb_main_menu_noop(cb: CallbackQuery) -> None:
    """Клик по неразрывной подписи страницы — просто снять «часики»."""
    await cb.answer()

async def _render_main_menu(cb: CallbackQuery, session: AsyncSession,
                            page: int = 0) -> None:
    if cb.message is None:
        await cb.answer("Открой бота командой /start 🙂", show_alert=True)
        return
    users = UserRepository(session)
    user = await users.get_or_create(cb.from_user.id, cb.from_user.first_name or "",
                                     cb.from_user.username)
    link = invite_link_for(user.tg_id)
    reward = get_settings().invite_reward_coins
    page %= len(MENU_PAGES)
    await safe_edit_or_answer(cb.message, _main_menu_text(user, page),
                              reply_markup=main_menu(link=link, reward=reward, page=page))
    await cb.answer()
