from __future__ import annotations

import contextlib

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
import html as _html
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pet, PetSpecies
from app.db.repositories import PetRepository, UserRepository
from app.keyboards.inline import (
    MENU_PAGES, main_menu, onboard_done, species_picker,
    start_pet_name_suggestions,
)
from app.services.achievements import AchievementService
from app.services.tamagotchi import SPECIES_DATA
from app.utils.formatting import progress_bar, xp_needed_for_level
from app.utils.safe_edit import safe_edit_or_answer
from app.config import get_settings
from loguru import logger
from app.middlewares.gate import (channel_link, gate_granted,
                                  is_channel_subscribed, reset_subscribe_cache)

router = Router(name="start")

PET_NAME_SUGGESTIONS = ["Барсик", "Мурка", "Персик", "Кузя", "Соня", "Имя своё…"]

class Onboarding(StatesGroup):
    choosing_pet_species = State()
    choosing_pet_name = State()

def species_picker_text() -> str:
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
    "Я — бот-компаньон канала {channel_name}. Здесь живут подписчики: "
    "общение, награды и немного магии 🐾\n\n"
    "Что тебя ждёт внутри:\n"
    "• 🧢 Мерч канала и предстоящие мероприятия;\n"
    "• 🐾 Питомец-тамагочи с играми и ⚔️ Ареной;\n"
    "• 🏆 Достижения, уровни, топы недели и 🖼 карточка профиля;\n"
    "• 🌦️ Живая погода и активность в чате — всё приносит XP и 🪙 монеты.\n\n"
    "{channel_line}"
    "Нажми «Начать», чтобы познакомиться поближе!"
)

def _channel_line() -> str:
    from app.middlewares.gate import channel_link
    ch, visual = channel_link()
    if not ch:
        return ""
    return f"📢 Наш канал: <a href=\"https://t.me/{ch}\">{visual}</a>\n"

def _channel_title() -> str:
    from app.middlewares.gate import channel_link
    ch, visual = channel_link()
    return visual or "нашего канала"

def invite_link_for(tg_id: int) -> str:
    ch = (get_settings().channel_username or "").strip().lstrip("@")
    if not ch:
        return ""
    return f"https://t.me/{ch}?start=invite_{tg_id}"

def _main_menu_text(user, page: int = 0) -> str:
    from app import themes

    need = xp_needed_for_level(user.level)
    bar = progress_bar(user.xp, need)
    ch, visual = channel_link()
    title, _actions = MENU_PAGES[page % len(MENU_PAGES)]
    # Тема оформления (например «🦇 Готика») может полностью заменять текст меню
    themed = themes.main_menu_renders(
        title=title, name=_html.escape(user.first_name or ''),
        level=user.level, bar=bar, xp=user.xp, need=need,
        coins=user.coins, streak=user.streak_days)
    if themed is not None:
        if ch:
            themed += f"\n\n🔔 Новости склепа: {visual} (t.me/{ch})"
        return themed
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
        lines += ["", f"📢 Новости канала: {visual} (t.me/{ch})"]
    return "\n".join(lines)

private_only = F.chat.type == "private"

def _menu_is_admin(user_id: int) -> bool:
    s = get_settings()
    if user_id in (s.admin_ids or []):
        return True
    return bool(s.merch_admin_id) and user_id == s.merch_admin_id

@router.message(CommandStart(), private_only)
async def cmd_start(message: Message, state: FSMContext, session: AsyncSession,
                    command: CommandObject | None = None,
                    sub_granted: bool = False) -> None:
    # Повторный /start всегда сбрасывает застрявшее FSM-состояние (например,
    # «подтверждение усыновления» или половину онбординга) — иначе меню не
    # открывается, а все кнопки уходят в зависший стейт и «не работают».
    await state.clear()
    if not sub_granted and not await is_channel_subscribed(
            message.bot, message.from_user.id):
        ch, visual = channel_link()
        lines = ["🔒 Взаимодействие с ботом недоступно:",
                 "ты не подписан на наш канал."]
        if ch:
            lines.append(f"\n📢 Подпишись ({visual}) — и возвращайся, я жду!")
        else:
            lines.append("\n📢 Подпишись на канал — и возвращайся, я жду!")
        lines.append("После подписки нажми «Проверить» или отправь /start.")
        from app.middlewares.gate import subscribe_kb
        await message.answer("\n".join(lines), reply_markup=subscribe_kb())
        return
    users = UserRepository(session)
    user = await users.get_or_create(
        tg_id=message.from_user.id,
        first_name=message.from_user.first_name or "",
        username=message.from_user.username,
    )
    if not user.onboarded:
        from app.db.models import Pet
        pet = (await session.execute(
            select(Pet).where(Pet.user_id == user.tg_id).limit(1)
        )).scalar_one_or_none()
        if pet is not None:
            user.pet_name = pet.name
            user.onboarded = True
            await session.commit()
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
    if not user.welcome_shown:
        from app import themes as _themes
        await message.answer(
            _themes.welcome_text(_html.escape(user.first_name or "друг"),
                                 _html.escape(_channel_title()),
                                 _channel_line()),
            parse_mode="HTML",
        )
        user.welcome_shown = True
        await session.commit()
    link = invite_link_for(user.tg_id)
    reward = get_settings().invite_reward_coins
    # Deep-link навигация из уведомлений: /start nav_<screen>_[arg] сразу
    # открывает нужный экран вместо главного меню. Сейчас используется в
    # уведомлении о новой броне мерча («📋 Все брони», «🧾 Бронь #id»).
    if payload.startswith("nav_"):
        handled = await _handle_nav_payload(payload, message, session)
        if handled:
            return
    text = _main_menu_text(user)
    await message.answer(text, reply_markup=await _menu_markup(
        link=link, reward=reward, is_admin=_menu_is_admin(message.from_user.id)),
        parse_mode="HTML")


async def _handle_nav_payload(payload: str, message: Message,
                              session: AsyncSession) -> bool:
    """Открыть экран по deep-link-адресату (см. merch._bot_link).

    Формат: ``nav_<screen>`` или ``nav_<screen>_<arg>``. Возвращает True,
    если экран открыт; False — неизвестный адресат (тогда покажем меню).
    """
    rest = payload[4:]  # срезать "nav_"
    # Токен навигации: «nav_merch_myres» → ("merch", "myres");
    # «nav_rescard_123» → ("rescard", "123").
    parts = rest.split("_", 1)
    screen = parts[0] if parts else ""
    arg = parts[1] if len(parts) > 1 else ""
    if screen == "merch" and arg == "myres":
        from app.handlers.merch import merch_my_reserves_open
        await merch_my_reserves_open(message, session, message.from_user.id)
        return True
    if screen == "rescard" and arg.isdigit():  # nav_rescard_<vid>
        from app.handlers.merch import merch_reserve_card_open
        await merch_reserve_card_open(message, session, int(arg),
                                      message.from_user.id)
        return True
    return False

async def _menu_markup(link: str | None = None, reward: int = 0, page: int = 0,
                       is_admin: bool = False):
    """Главное меню + подпись актуальной погоды на кнопке «Погода».

    Метка берётся из кэша сервиса погоды (weather_button_label), поэтому
    сборка меню не делает лишних сетевых запросов и не может зависнуть.
    """
    from app.services.weather import weather_button_label
    return main_menu(link=link, reward=reward, page=page, is_admin=is_admin,
                     weather_label=await weather_button_label())

@router.callback_query(F.data == "gate:check")
async def cb_gate_check(cb: CallbackQuery, bot: Bot, session: AsyncSession,
                        state: FSMContext) -> None:
    reset_subscribe_cache(cb.from_user.id)
    await state.clear()
    if not await is_channel_subscribed(bot, cb.from_user.id):
        uid = cb.from_user.id
        diag = []
        with contextlib.suppress(Exception):
            from app.middlewares.gate import _registry_row_state, required_chats
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
        reply_markup=await _menu_markup(link=invite_link_for(user.tg_id),
                                        reward=get_settings().invite_reward_coins))
    await cb.answer("Ура, добро пожаловать! 🎉")

@router.callback_query(F.data == "onb:start")
async def cb_onboard_start(cb: CallbackQuery, state: FSMContext,
                           session: AsyncSession, bot: Bot) -> None:
    if not await is_channel_subscribed(bot, cb.from_user.id):
        from app.middlewares.gate import subscribe_kb
        await cb.answer("Сначала подпишись на канал 📢", show_alert=True)
        with contextlib.suppress(Exception):
            await safe_edit_or_answer(
                cb.message,
                "🔒 Взаимодействие с ботом недоступно:\nты не подписан ни на наш "
                "канал, ни на группу обсуждения.\n\n📢 Подпишись — и возвращайся, "
                "я жду!\nПосле подписки нажми «Проверить».",
                reply_markup=subscribe_kb())
        return
    users = UserRepository(session)
    user = await users.get_or_create(cb.from_user.id, cb.from_user.first_name or "",
                                     cb.from_user.username)
    if user.onboarded:
        await safe_edit_or_answer(cb.message, _main_menu_text(user),
                                  reply_markup=await _menu_markup(
                                      link=invite_link_for(user.tg_id),
                                      reward=get_settings().invite_reward_coins))
        await cb.answer()
        return
    await state.clear()
    await safe_edit_or_answer(cb.message, "Отлично! Всё уже открыто в меню ниже 👇",
                              reply_markup=main_menu())
    await cb.answer()

@router.callback_query(F.data == "onb:skip")
async def cb_onboard_skip(cb: CallbackQuery, state: FSMContext,
                          session: AsyncSession) -> None:
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

async def _picker_screen(state: FSMContext, message=None, cb=None,
                         suppress_back: bool = False) -> None:
    await state.set_state(Onboarding.choosing_pet_species)
    text = ("🐣 Шаг 1 из 3. Выбери питомца — у каждого свой характер и бонусы:\n\n"
            + species_picker_text())
    chat_id = (cb.message.chat.id if cb is not None and cb.message
               else message.chat.id if message is not None else None)
    # suppress_back=True — экран перерисован кнопкой «Назад» с шага имени:
    # повторный «Назад → onb:species_back» был бы самопетлёй, подавляем его
    # (back_cb="" означает «только 🏠 Меню»).
    back_cb = "" if suppress_back else None
    if cb is not None:
        await safe_edit_or_answer(cb.message, text,
                                  reply_markup=species_picker(chat_id, back_cb))
    elif message is not None:
        await message.answer(text, reply_markup=species_picker(chat_id, back_cb))

@router.callback_query(F.data == "onb:species_back")
async def cb_species_back(cb: CallbackQuery, state: FSMContext) -> None:
    # «Назад» с шага 2 (имя) — перерисовать шаг 1 (выбор вида). На самом
    # шаге 1 кнопка «Назад» строится с явным подавлением (suppress_back),
    # чтобы не получилось петли «Назад → onb:species_back → тот же экран».
    await _picker_screen(state, cb=cb, suppress_back=True)
    await cb.answer()

@router.callback_query(F.data.startswith("onb:species:"))
async def cb_pick_species(cb: CallbackQuery, state: FSMContext) -> None:
    code = cb.data.split(":")[2]
    if code not in SPECIES_DATA:
        # Повторный вход в онбординг с экрана, где уже показан пикер вида
        # (например, «🥚 Усыновить питомца» из CTA-экрана «нет питомца»,
        # который сам лежит на шаге 1): перерисовываем шаг 1 вместо ошибки
        # «Такого питомца нет». Кнопки «Усыновить» на этих экранах — это
        # вход в раздел, а не выбор вида.
        cur = await state.get_state()
        if cur == str(Onboarding.choosing_pet_species.state):
            await _picker_screen(state, cb=cb)
            await cb.answer()
            return
        await cb.answer("Такого питомца нет", show_alert=True)
        return
    await state.update_data(species=code)
    await state.set_state(Onboarding.choosing_pet_name)
    # «Имя своё…» — это не имя, а приглашение написать своё сообщением:
    # сразу просим текст, чтобы не показывать тот же список suggestions
    # (визуальная самопетля).
    if code == "Имя своё…":
        await safe_edit_or_answer(cb.message, "✍️ Напиши своё имя питомца сообщением:")
    else:
        await safe_edit_or_answer(cb.message, 
            f"{SPECIES_DATA[code]['emoji']} Отличный выбор — {SPECIES_DATA[code]['title']}!\n\n"
            "Шаг 2 из 3. Выбери имя питомцу (или напиши своё сообщением):",
            reply_markup=start_pet_name_suggestions(PET_NAME_SUGGESTIONS,
                                                    cb.message.chat.id),
        )
    await cb.answer()

@router.message(Onboarding.choosing_pet_species, F.text & ~F.text.startswith("/"))
async def msg_stuck_in_species(message: Message, state: FSMContext) -> None:
    await _picker_screen(state, message=message)

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
    await _render_main_menu(cb, session, state, page=0)

@router.callback_query(F.data.startswith("menu:page:"))
async def cb_main_menu_page(cb: CallbackQuery, session: AsyncSession,
                            state: FSMContext) -> None:
    if await state.get_state() is not None:
        await state.clear()
    try:
        page = int(cb.data.split(":")[-1])
    except ValueError:
        page = 0
    await _render_main_menu(cb, session, state, page=page)

@router.callback_query(F.data == "menu:noop")
async def cb_main_menu_noop(cb: CallbackQuery) -> None:
    await cb.answer()

# Совместимость со старыми клавиатурами: в боте никогда не было обработчика
# «menu:home» (домашняя кнопка — это «menu:main»). Если у пользователя в чате
# осталось сообщение со старой разметки, нажатие такой кнопки раньше уходило в
# catch-all и показывало «Кнопка устарела». Теперь просто рендерим главное меню.
@router.callback_query(F.data == "menu:home")
async def cb_main_menu_home_alias(cb: CallbackQuery, session: AsyncSession,
                                  state: FSMContext) -> None:
    await cb_main_menu(cb, session, state)

async def _render_main_menu(cb: CallbackQuery, session: AsyncSession,
                            state: FSMContext | None, page: int = 0) -> None:
    if cb.message is None:
        await cb.answer("Открой бота командой /start 🙂", show_alert=True)
        return
    users = UserRepository(session)
    user = await users.get_or_create(cb.from_user.id, cb.from_user.first_name or "",
                                     cb.from_user.username)
    link = invite_link_for(user.tg_id)
    reward = get_settings().invite_reward_coins
    # «Меню» — это всегда главное меню: раньше у неонборднутых без питомца оно
    # перескакивало на экран выбора питомца, из-за чего «Назад/Меню» из любого
    # раздела (например, из мерча) вёл в онбординг. Кнопки «Усыновить» в самом
    # меню тоже нет: усыновление — внутри раздела питомца («🐾 Питомец» → хаб),
    # где без питомца показывается экран с предложением завести его.
    page %= len(MENU_PAGES) + (1 if _menu_is_admin(cb.from_user.id) else 0)
    await safe_edit_or_answer(cb.message, _main_menu_text(user, min(page, len(MENU_PAGES) - 1)),
                              reply_markup=await _menu_markup(
                                  link=link, reward=reward, page=page,
                                  is_admin=_menu_is_admin(cb.from_user.id)))
    await cb.answer()
