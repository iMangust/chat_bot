from __future__ import annotations

import random
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pet
from app.db.repositories import PetRepository, UserRepository
from app.i18n import t
from app.keyboards.inline import (PET_PAGES, adopt_cta_kb,
                                   pet_history_kb, pet_hub,
                                   pet_page_count, train_menu)
from app.utils.safe_edit import safe_edit_or_answer, answer_safe
from app.utils.html_text import esc
from app.services.tamagotchi import (SPECIES_DATA, TamagotchiService, _aware,
                                     _species_key)
from app.services.weather import (STAT_LEGEND as _STAT_LEGEND,
                                   walk_forecast_line, weather_hint_block)
from app.utils.local_time import now as local_now

router = Router(name="tamagotchi")
_aware_dt = _aware

class AdoptConfirm(StatesGroup):
    confirm = State()

async def _get_pet(session: AsyncSession, tg_id: int) -> Pet | None:
    return await PetRepository(session).get_by_user(tg_id)

_PET_PAGE_CTX: dict[int, int] = {}

def pet_page_for(chat_id: int) -> int:
    return _PET_PAGE_CTX.get(int(chat_id), 0) % max(1, pet_page_count())

def set_pet_page(chat_id: int, page: int) -> int:
    page %= max(1, pet_page_count())
    _PET_PAGE_CTX[int(chat_id)] = page
    return page

def _collect_walk_result(svc: TamagotchiService, pet: Pet, session: AsyncSession):
    if pet.walk_until is None:
        return None
    if local_now() < _aware_dt(pet.walk_until):
        return None
    text, coins, xp = svc.finish_walk_event(pet)
    return text, coins, xp

HELP_TEXT = (
    "🐾 <b>Как устроен бот: полный гид</b>\n\n"

    "<b>📌 Основные команды</b>\n"
    "/start — главное меню (здесь все разделы)\n"
    "/pet — карточка питомца с поведением, статами и погодными эффектами\n"
    "/weather — живая погода Камчатки + разбор плюсов/минусов для питомца\n"
    "/stats — твоя статистика: уровень, XP, монеты, стрик\n"
    "/ach — 🏆 достижения (листать кнопками ◀️/▶️)\n"
    "/top — 🏅 топы чатов по активности\n"
    "/arena — ⚔️ арена питомцев (дуэли)\n"
    "/card — 🖼 PNG-карточка профиля\n"
    "/award — 🎁 итоги недели и награды\n"
    "/settings — ⚙️ настройки уведомлений\n"
    "/help — эта справка\n\n"

    "<b>💰 Как зарабатывать XP и 🪙 монеты</b>\n"
    "• Пиши в подключённых чатах: текст, фото, голосовые, «кружки»,\n"
    "  стикеры — каждый тип считается отдельно, но с кулдауном ~1–2 мин;\n"
    "• Ставь реакции на сообщения собеседников (есть дневной лимит);\n"
    "• Активность в канале, если бот там админ;\n"
    "• Ежедневный стрик: чем дольше заходишь — тем больше множитель.\n"
    "Уровень растёт от XP; с уровнем открываются награды и возможности Арены.\n\n"

    "<b>🐱 Питомец: базовые механики</b>\n"
    f"Пять статов: {_STAT_LEGEND}\n"
    "Они падают со временем (зимой быстрее, летом веселее). Если сытость или\n"
    "гигиена уйдут ниже 20 — питомец болеет; при критических значениях help\n"
    "падает, а ты получишь напоминание. Лечится предметом «💊 Лекарство» из магазина.\n\n"
    "<b>Действия в хабе питомца (кнопки листаются ◀️/▶️):</b>\n"
    "• 🍎 Покормить — бесплатно (хлеб) или едой из инвентаря; кулдаун 60 сек;\n"
    "• 🛁 Помыть — восстанавливает гигиену;\n"
    "• 🎮 Игры — мини-игры (камни-ножницы-бумага, угадайка, «21»): +😊 Счастье, −⚡ Энергия;\n"
    "• 🏋️ Тренировки — качают 💪 силу, 🐾 ловкость или 🧠 интеллект (нужна энергия);\n"
    "• 🌳 Прогулка — уходит на 2 часа, вернётся с монетами, XP и случайным событием;\n"
    "• 😴 Сон — восстанавливает энергию (+8/час), разбудить можно досрочно.\n"
    "Питомец растёт: 🥚 яйцо → 👶 малыш → 🧑 подросток → 🐉 взрослый → 👑 легенда.\n"
    "Вид важен: 🐈 кот любит игры, 🐕 пёс — прогулки, 🦊 лиса приносит больше монет,\n"
    "🦉 сова получает больше XP с тренировок, 🐉 дракон — универсал (500 🪙).\n\n"

    "<b>🛒 Магазин и 🎒 инвентарь</b>\n"
    "Открывается из хаба питомца (страница «🎒 Вещи»). Там еда и энергетики,\n"
    "лекарства, аксессуары и окрасы. Страницы листаются кнопками ◀️ ▶️ —\n"
    "после покупки ты останешься на той же странице. Купленное ложится в\n"
    "«🎒 Инвентарь», откуда предмет можно применить к питомцу (кнопка «Использовать»). \n"
    "Еда и напитки дают временные бафы: кофеин (энергия не тратится), «Сытный час» и т.п.\n\n"

    "<b>🌦️ Живая погода Камчатки (влияет на всё!)</b>\n"
    "Бот берёт реальные данные OpenWeather (openweathermap.org)\nдля Петропавловска-Камчатского:\n"
    "темп., ощущаемая темп., влажность, ветер и ПОРЫВЫ, осадки, день/ночь —\n"
    "обновление в фоне каждые 3 часа (ответы бота мгновенные — из кэша).\n"
    "Каждая погода меняет жизнь питомца (тик — раз в 3 ч; имена статов едины:\n"
    "🍎 Сытость, 😊 Счастье, ⚡ Энергия, 🫧 Гигиена, ❤️ Здоровье):\n"
    "• ☀️ Ясно — пассивный баф (+5 😊 Счастье, +3 ⚡ Энергия за тик); гулять ПОЛЕЗНО:\n"
    "  находки и XP ×1.3, +8 😊 Счастья, +5 ⚡ Энергии, шанс +1 к 💪/🏃 прямо на прогулке;\n"
    "• ⛅ Переменная облачность — слегка позитивно (+1/+1 за тик); прогулка комфортная (×1.1);\n"
    "• ☁️ Пасмурно — сплошная облачность: лёгкая хандра (−3 😊 Счастья за тик, −24 в сутки);\n"
    "• 🌫️ Туман — вялость: −2 😊 Счастья, −3 ⚡ Энергии за тик, риск простуды 3%;\n"
    "• 🌧️ Дождь — грусть и грязь: −4 😊 Счастья, −2 ⚡ Энергии, −3 🫧 Гигиены за тик,\n"
    "  риск простуды 8%; на прогулке −15% находок и 15% шанс вернуться больным;\n"
    "• ❄️ Снег — тянет в сон и есть хочется (−3 😊 Счастья/−3 ⚡ Энергии, +3 🍎 Сытости);\n"
    "  прогулка весёлая (снежки!), но с риском 12%; при −7°C и ниже переходит в «мороз»;\n"
    "• ⛈️ Гроза (−6/−4) / 🥶 мороз или ветер ≥11 м/с (−4/−3) — сильный дебаф, риск до 25%;\n"
    "  бот честно советует: сиди дома 🏠;\n"
    "• 🌙 Ночью — общий дебаф бодрости: дополнительно −2 😊 Счастья и −2 ⚡ Энергии.\n"
    "Перед кнопкой «🚶 Прогулка» бот покажет прогноз-окно на ближайшие 3 часа\n"
    "(☀️ → 🌧️ → ⛈️) и предупредит, если погода портится. Простуда лечится\n"
    "«💊 Лекарство» из магазина; экипировка-«щиты» снижает риск болезни.\n"
    "Команда /weather покажет живую погоду и полный разбор её плюсов/минусов;\n"
    "в карточке питомца и при прогулке эффекты расписаны прямо на экране.\n"
    "Если сеть недоступна — сезонная модель без выдумывания погоды.\n\n"

    "<b>🏆 Достижения</b>\n"
    "Прогресс обновляется автоматически: за сообщения, реакции, монеты, а также\n"
    "за действия с питомцем (кормления, прогулки, уровень). Новые ачивки приходят\n"
    "уведомлением. Спорные: «Покормить 100 раз», «Вырастить до 10 уровня» и др.\n\n"

    "<b>⚔️ Арена и 🐾 друзья</b>\n"
    "На Арене питомцы сражаются (сила/ловкость/интеллект + экипировка + бафы).\n"
    "Победа = монеты и XP. У питомцев могут появляться «друзья» с других аккаунтов —\n"
    "каждый друг даёт +1 😊 Счастье в сутки.\n\n"

    "<b>⚙️ Настройки и уведомления</b>\n"
    "/settings — тумблеры напоминаний (голод, болезнь, итоги недели).\n"
    "Бот не спамит: между однотипными напоминаниями пауза несколько часов.\n\n"
    "Есть вопрос? Просто напиши в чат — или снова /start 🙂"
)

def _split_html(text: str, limit: int = 4000) -> list[str]:
    tag_re = re.compile(r"</?(b|i|u|s|code|pre|tg-spoiler)>")
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    open_stack: list[str] = []

    def close_part(part: str, stack: list[str]) -> str:
        for tag in reversed(stack):
            part += f"</{tag}>"
        return part

    def reopen(stack: list[str]) -> str:
        return "".join(f"<{t}>" for t in stack)

    def flush() -> None:
        nonlocal cur, cur_len
        if not cur:
            return
        part = close_part("\n".join(cur), open_stack)
        chunks.append(part)
        cur = [reopen(open_stack)] if open_stack else []
        cur_len = len(cur[0]) if cur else 0

    for line in text.split("\n"):
        add = len(line) + (1 if cur else 0)
        if cur and cur_len + add > limit:
            flush()
        cur.append(line)
        cur_len += add
        for m in tag_re.finditer(line):
            name = m.group(1)
            if m.group(0).startswith("</"):
                if name in open_stack:
                    open_stack.remove(name)
            else:
                open_stack.append(name)
    flush()
    return [c for c in (ch.strip("\n") for ch in chunks) if c]

@router.message(Command("help"), F.chat.type == "private")
async def cmd_help(message: Message) -> None:
    from app.utils.text_split import split_message, strip_html_tags
    try:
        for chunk in _split_html(HELP_TEXT):
            await message.answer(chunk, parse_mode="HTML")
    except Exception:
        plain = strip_html_tags(HELP_TEXT)
        for chunk in split_message(plain):
            await message.answer(chunk)

def _weather_error_text(exc: BaseException) -> str:
    """Человекочитаемое сообщение об ошибке погоды + лог полного трейсбека.

    Раньше наружу уходило только имя класса исключения (например «NameError»),
    по которому невозможно было понять, что сломалось. Теперь стек-трейс
    пишется в лог, а пользователю показывается короткая подсказка.
    """
    logger.exception("weather screen failed: {}", exc)
    if isinstance(exc, NameError):
        hint = "Похоже, бот запущен из устаревшего кода — перезапусти его."
    else:
        hint = "Данные обновляет сервер — загляни чуть позже."
    return f"🌦️ Погода временно недоступна. {hint}"


async def _weather_text() -> str:
    """Собирает текст погоды для команды /weather и кнопки меню.

    ВАЖНО: все обращения к сервису погоды — ВНУТРИ try. Импорт тоже может
    упасть (например, в запущенном процессе лежит устаревшая версия модуля
    без нужных имён), и раньше это исключение уходило наружу до обработчика,
    из-за чего пользователь видел «Погода временно недоступна (NameError)».
    """
    try:
        from app.services.weather import kamchatka_weather, weather_hint_block_fresh
        w = await kamchatka_weather()
        hint = await weather_hint_block_fresh(walk=True, show_legend=True)
    except Exception as exc:
        return _weather_error_text(exc)
    text = f"🌦️ Погода на Камчатке: {w['icon']} {w['name']}\n{w['note']}"
    if hint:
        text += "\n\n📋 Как это влияет на питомца:\n" + hint
    else:
        text += "\n\n⚠️ Реальные данные недоступны — действует сезонная модель," \
                " погодные эффекты и риски простуды отключены."
    return text


@router.message(Command("weather", "погода"), F.chat.type == "private")
async def cmd_weather(message: Message) -> None:
    await message.answer(await _weather_text())


@router.callback_query(F.data == "menu:weather")
async def cb_menu_weather(cb: CallbackQuery) -> None:
    # Кнопка погоды в главном меню — вход в полноценный погодный раздел:
    # «Сегодня» (актуальный снимок + почасовая лента) / «Неделя» (прогноз
    # по дням). Подпись самой кнопки показывает актуальную погоду.
    await _render_weather_screen(cb, view="today", force=False)
    await cb.answer()


@router.callback_query(F.data.in_({"wthr:today", "wthr:week"}))
async def cb_weather_view(cb: CallbackQuery) -> None:
    view = "week" if cb.data.endswith("week") else "today"
    await _render_weather_screen(cb, view=view, force=False)
    await cb.answer()


@router.callback_query(F.data.startswith("wthr:refresh:"))
async def cb_weather_refresh(cb: CallbackQuery) -> None:
    # Кнопка «🔄 Обновить» убрана из клавиатуры погоды: обновление выполняет
    # серверный планировщик (job weather_updater). Этот обработчик оставлен
    # только для «висячих» кнопок в старых сообщениях — он НЕ дергает API,
    # а показывает актуальный кэш, чтобы клики не создавали спам запросов.
    view = cb.data.split(":")[-1]
    if view not in ("today", "week"):
        view = "today"
    await _render_weather_screen(cb, view=view, force=False)
    await cb.answer("Погоду обновляет сервер 🌦️")


async def _render_weather_screen(cb: CallbackQuery, view: str,
                                 force: bool = False) -> None:
    # Все обращения к сервису погоды (включая импорт — на случай устаревшего
    # кода в запущенном процессе) внутри try: наружу не должен уходить
    # NameError/ImportError, пользователю показывается понятная заглушка.
    try:
        from app.keyboards.inline import weather_kb
        # ВАЖНО: weather_hint_block_fresh обязательно в этом же импорте —
        # раньше он здесь отсутствовал и экран падал с NameError (см. коммит
        # 501867a). Все имена, используемые ниже, должны быть в списке импорта.
        from app.services.weather import (_ensure_fresh, hourly_points,
                                          render_today, render_week,
                                          weather_now, _day_rows,
                                          weather_hint_block_fresh)
        if force:
            await _ensure_fresh(force=True)
        w = await weather_now()
        hint = await weather_hint_block_fresh(show_legend=True)
        footer = ("\n\n📋 Как это влияет на питомца:\n" + hint) if hint else ""
        if view == "week":
            body = render_week(_day_rows(await hourly_points()))
        else:
            body = render_today(w, await hourly_points())
        text = body + footer
        kb = weather_kb(view)
    except Exception as exc:
        text = _weather_error_text(exc)
        kb = None
    await safe_edit_or_answer(cb.message, text, reply_markup=kb)

@router.message(Command("pet"), F.chat.type == "private")
async def cmd_pet(message: Message, session: AsyncSession) -> None:
    svc = TamagotchiService(session)
    pet = await _get_pet(session, message.from_user.id)
    if pet is None:
        from app.keyboards.inline import adopt_cta_kb
        await message.answer(
            "🥚 У тебя пока нет питомца! Заведи его прямо сейчас — "
            "выбери вид и имя:",
            reply_markup=adopt_cta_kb())
        return
    await svc.apply_decay(pet)
    users = UserRepository(session)
    user = await users.get(message.from_user.id)
    await answer_safe(message,
                      await svc.render_async(pet, user.first_name if user else ""),
                      reply_markup=pet_hub(pet_page_for(message.chat.id),
                                           sleeping=pet.is_sleeping,
                                           walking=svc.on_walk(pet)))

@router.callback_query(F.data == "menu:pet")
async def pet_screen(cb: CallbackQuery, session: AsyncSession,
                     state: FSMContext) -> None:
    # «Питомец» — точка входа раздела из главного меню. Если пользователь
    # сидит в шагах онбординга (например, дошёл до выбора имени), нажатие
    # должно выйти из FSM и показать обычный экран (питомца или CTA
    # «усыновить»), а не угодить в заглушку без ответа.
    if await state.get_state() is not None:
        await state.clear()
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        from app.keyboards.inline import adopt_cta_kb
        await safe_edit_or_answer(cb.message,
            "🥚 У тебя пока нет питомца! Заведи его прямо сейчас — "
            "выбери вид и имя:",
            reply_markup=adopt_cta_kb(),
        )
        await cb.answer()
        return
    await svc.apply_decay(pet)
    users = UserRepository(session)
    user = await users.get(cb.from_user.id)
    text = await svc.render_async(pet, user.first_name if user else "")
    await safe_edit_or_answer(cb.message, text,
                              reply_markup=pet_hub(pet_page_for(cb.message.chat.id),
                                                   sleeping=pet.is_sleeping,
                                                   walking=svc.on_walk(pet)))
    await cb.answer()

@router.callback_query(F.data == "pet:noop")
async def pet_hub_noop(cb: CallbackQuery) -> None:
    await cb.answer()

@router.callback_query(F.data == "pet:page:0")
@router.callback_query(F.data.startswith("pet:page:"))
async def pet_page_screen(cb: CallbackQuery, session: AsyncSession) -> None:
    try:
        page = int(cb.data.split(":")[-1])
    except ValueError:
        page = 0
    page = set_pet_page(cb.message.chat.id, page)
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await safe_edit_or_answer(cb.message,
            "🥚 У тебя пока нет питомца — но играть всё равно можно!\n\n"
            "• 🏅 Топы и 📊 Статы работают без питомца;\n"
            "• питомца можно завести в любой момент кнопкой ниже;\n"
            "• если передумаешь — в ⚙️ Настройках есть «🐣 Завести питомец».",
            reply_markup=adopt_cta_kb(),
        )
        await cb.answer()
        return
    await svc.apply_decay(pet)
    title = PET_PAGES[page][0]
    hint = {0: "Здесь базовый уход — делай его каждый день ✨",
            1: "Предметы, покупки и гардероб питомца 🎒",
            2: "Развлечения и социалка: игры, прогулки, друзья, бои 🥊"}[page]
    crit = svc.is_critical(pet)
    extra = ""
    if crit:
        cost = svc.revive_cost(pet)
        extra = ("\n\n⚠️ Обычный уход заблокирован — спасай реанимацией "
                 f"за {cost} 🪙 или усыновляй нового." if cost > 0 else
                 "\n\n⚠️ Жизней больше нет — можно только усыновить нового.")
    await safe_edit_or_answer(
        cb.message,
        f"{await svc.render_async(pet)}\n\n<i>{title} · {hint}</i>{extra}",
        reply_markup=pet_hub(page, critical=crit, sleeping=pet.is_sleeping,
                             walking=svc.on_walk(pet)),
    )
    await cb.answer()

@router.callback_query(F.data == "pet:revive")
async def act_revive(cb: CallbackQuery, session: AsyncSession) -> None:
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    await svc.apply_decay(pet)
    if not svc.is_critical(pet):
        await safe_edit_or_answer(
            cb.message, await svc.render_async(pet),
            reply_markup=pet_hub(pet_page_for(cb.message.chat.id),
                                 sleeping=pet.is_sleeping,
                                 walking=svc.on_walk(pet)))
        return await cb.answer(t("pet.not_critical"), show_alert=True)
    users = UserRepository(session)
    user = await users.get(cb.from_user.id)
    cost = svc.revive_cost(pet)
    if cost < 0:
        await safe_edit_or_answer(
            cb.message,
            f"{await svc.render_async(pet)}\n\n" + t("pet.no_more_lives", name=esc(pet.name)),
            reply_markup=pet_hub(0, critical=True))
        return await cb.answer()
    have = user.coins if user else 0
    if have < cost:
        if user and await svc.free_revive_for_newbie(pet):
            await PetRepository(session).log_action(pet.id, "revive_free")
            await session.commit()
            return await cb.answer("🎁 Первая реанимация — бесплатная! Береги питомца 💖",
                                   show_alert=True)
        await session.commit()
        return await cb.answer(
            t("pet.revive_no_money", need=cost, have=have), show_alert=True)
    result = await svc.revive(pet)
    await users.add_coins(user.tg_id, -cost)
    await PetRepository(session).log_action(pet.id, "revive", value=cost)
    await session.commit()
    await safe_edit_or_answer(
        cb.message,
        f"{result}\n\n" + await svc.render_async(pet),
        reply_markup=pet_hub(pet_page_for(cb.message.chat.id)))
    await cb.answer(f"⭐ −{cost}")

@router.callback_query(F.data == "pet:adopt_confirm", AdoptConfirm.confirm)
async def pet_adopt_confirm(cb: CallbackQuery, session: AsyncSession,
                            state: FSMContext) -> None:
    svc = TamagotchiService(session)
    data = await state.get_data()
    repo = PetRepository(session)
    current = await repo.get_by_user(cb.from_user.id)
    if current and current.id == data.get("pet_id"):
        await svc.archive_pet(session, current, reason="rehomed")
        await session.commit()
    from app.handlers.start import Onboarding
    from app.keyboards.inline import species_picker
    # Не сбрасываем всё состояние (state.clear() затирал pet_id/species и
    # ломало онбординг) — просто переводим FSM на шаг выбора вида.
    await state.set_state(Onboarding.choosing_pet_species)
    await safe_edit_or_answer(
        cb.message,
        "🐣 Прежний питомец пристроен в историю. Выбери нового:\n\n"
        + _species_picker_text(),
        reply_markup=species_picker(cb.message.chat.id))
    await cb.answer()

@router.callback_query(F.data == "pet:adopt_cancel", AdoptConfirm.confirm)
async def pet_adopt_cancel(cb: CallbackQuery, session: AsyncSession,
                           state: FSMContext) -> None:
    await state.clear()
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    svc = TamagotchiService(session)
    await safe_edit_or_answer(cb.message, await svc.render_async(pet),
                              reply_markup=pet_hub(pet_page_for(cb.message.chat.id)))
    await cb.answer("Отменено 👍")

@router.callback_query(F.data == "pet:adopt")
async def pet_adopt_screen(cb: CallbackQuery, session: AsyncSession,
                           state: FSMContext) -> None:
    # ВАЖНО: этот хендлер зарегистрирован без FSM-фильтра, поэтому он ловит
    # нажатие «Усыновить» и на экране подтверждения (AdoptConfirm.confirm),
    # и в онбординге (Onboarding.choosing_pet_species). Без cb.answer() в этих
    # ветках кнопка «визжала» бесконечно и казалась «не рабочей».
    from app.handlers.start import Onboarding
    cur_state = await state.get_state()
    if cur_state == str(AdoptConfirm.confirm.state):
        await cb.answer("Сначала подтверди или отмени прошлое действие 👆",
                        show_alert=True)
        return
    if cur_state == str(Onboarding.choosing_pet_species.state):
        from app.keyboards.inline import species_picker
        await safe_edit_or_answer(
            cb.message,
            "🐣 Выбери питомца — у каждого свой характер и бонусы:\n\n"
            + _species_picker_text(),
            reply_markup=species_picker(cb.message.chat.id))
        await cb.answer()
        return
    if cur_state == str(Onboarding.choosing_pet_name.state):
        await cb.answer("Сначала выбери вид питомца 👆", show_alert=True)
        return
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        users = UserRepository(session)
        user = await users.get_or_create(cb.from_user.id, cb.from_user.first_name or "",
                                         cb.from_user.username)
        user.onboarded = True
        await session.commit()
        await state.set_state(Onboarding.choosing_pet_species)
        from app.keyboards.inline import species_picker
        await safe_edit_or_answer(
            cb.message,
            "🐣 Выбери питомца — у каждого свой характер и бонусы:\n\n"
            + _species_picker_text(),
            reply_markup=species_picker(cb.message.chat.id))
        await cb.answer()
        return
    await state.set_state(AdoptConfirm.confirm)
    await state.update_data(pet_id=pet.id)
    from app.keyboards.inline import adopt_confirm_kb
    await safe_edit_or_answer(
        cb.message,
        f"⚠️ Ты уверен, что хочешь усыновить нового питомца?\n\n"
        f"Текущий — <b>{esc(pet.name)}</b> (ур. {pet.level}, поколении "
        f"{pet.generation}) — уйдёт в историю 📜: его уровень, ачивки и логи "
        "сохранятся, но прогресс не перенесётся.\n\n"
        "Подтверди действие кнопкой ниже или отмени его.",
        reply_markup=adopt_confirm_kb())
    await cb.answer()

def _species_picker_text() -> str:
    from app.handlers.start import species_picker_text
    return species_picker_text()

@router.callback_query(F.data == "pet:history")
async def pet_history_screen(cb: CallbackQuery, session: AsyncSession) -> None:
    svc = TamagotchiService(session)
    pets = await svc.history(session, cb.from_user.id)
    current = await _get_pet(session, cb.from_user.id)
    if not pets:
        text = ("📜 <b>История питомцев</b>\n\n" + t("pet.history_empty"))
    else:
        lines = [t("pet.history_title"), ""]
        for p in pets:
            when = _aware(p.archived_at).strftime("%d.%m.%Y") if p.archived_at else "?"
            reason = {"rehomed": "усыновлён (смена питомца)",
                      "grew_up": "вырос и улетел 🕊"}.get(p.archive_reason or "", "в архиве")
            lines.append(f"🐾 Поколение {p.generation}: <b>{esc(p.name)}</b> "
                         f"· ур. {p.level} · {p.species.value if hasattr(p.species, 'value') else p.species}"
                         f"\n   ↳ {reason}, {when}")
        text = "\n".join(lines)
    await safe_edit_or_answer(cb.message, text,
                              reply_markup=pet_history_kb(
                                  has_current=current is not None,
                                  chat_id=cb.message.chat.id if cb.message else None))
    await cb.answer()

async def _after_action(cb: CallbackQuery, session: AsyncSession, result_text: str,
                        *, fx: str | None = None, stat: str | None = None) -> None:
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await cb.answer("Питомец не найден", show_alert=True)
        return
    prefix = ""
    res = _collect_walk_result(svc, pet, session)
    if res:
        wtext, coins, xp = res
        pet.walk_until = None
        pet.walk_start_at = None
        if coins:
            users = UserRepository(session)
            user = await users.get(cb.from_user.id)
            if user:
                user.coins += coins
        await svc.add_pet_xp(pet, xp)
        await PetRepository(session).log_action(pet.id, "walk_done", value=coins)
        prefix = f"{wtext}\n\n"
    try:
        await safe_edit_or_answer(cb.message, 
            f"{prefix}{result_text}\n\n" + await svc.render_async(pet),
            reply_markup=pet_hub(pet_page_for(cb.message.chat.id),
                                 sleeping=pet.is_sleeping,
                                 walking=svc.on_walk(pet)),
        )
    finally:
        await session.commit()

    try:
        from app.services.activity import ActivityService
        from app.services.achievements import AchievementService
        user = await UserRepository(session).get(cb.from_user.id)
        if user is not None:
            counters = await ActivityService(session)._counters(user, local_now())
            newly = await AchievementService(session).check(cb.from_user.id, counters)
            await session.commit()
            if newly:
                logger.info("user {} unlocked via pet action: {}", cb.from_user.id,
                            [a.code for a in newly])
    except Exception as exc:
        logger.warning("achievement re-check after pet action failed: {}", exc)

    if fx:
        from app.utils.fx import apply_effect
        toast = result_text.split("\n")[0].strip()
        await apply_effect(cb, fx, stat=stat,
                           toast_override=toast[:200] or None)
    else:
        await cb.answer()

@router.callback_query(F.data == "pet:feed")
async def act_feed(cb: CallbackQuery, session: AsyncSession) -> None:
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    # Страж состояний: спящего не кормят, гуляющего дома нет.
    deny = svc.state_deny(pet, "feed")
    if deny:
        return await cb.answer(deny, show_alert=True)
    result = await svc.feed(pet, {"hunger": 15})
    fed = "Ням-ням" in result
    if fed:
        await PetRepository(session).log_action(pet.id, "feed")
    await _after_action(cb, session, result, fx="feed" if fed else None)

@router.callback_query(F.data == "pet:play")
async def act_play(cb: CallbackQuery, session: AsyncSession) -> None:
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    deny = svc.state_deny(pet, "play")
    if deny:
        return await cb.answer(deny, show_alert=True)
    win_chance = 0.45 + pet.agility * 0.01
    won = random.random() < min(win_chance, 0.85)
    result = await svc.play(pet, won)
    if "😴" in result or "🚶" in result:   # страж сработал внутри svc.play
        return await _after_action(cb, session, result)
    await PetRepository(session).log_action(pet.id, "play", value=int(won))
    await _after_action(cb, session, result, fx="win" if won else "lose")

@router.callback_query(F.data == "pet:sleep")
async def act_sleep(cb: CallbackQuery, session: AsyncSession) -> None:
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    # Страж состояний: гуляющий питомец не ляжет спать (его надо вернуть).
    deny = svc.state_deny(pet, "sleep")
    if deny and pet.is_sleeping:
        deny = svc.sleeping_hint("wake") or deny  # «уже спит» — мягкая подсказка
    if deny:
        return await cb.answer(deny, show_alert=True)
    pet_was_sleeping = pet.is_sleeping
    if pet.is_sleeping:
        result = await svc.wake(pet)
    else:
        result = await svc.sleep(pet, hours=8)
    await PetRepository(session).log_action(pet.id, "sleep")
    await _after_action(cb, session, result, fx="wake" if pet_was_sleeping else "sleep")

@router.callback_query(F.data == "pet:wake")
async def act_wake(cb: CallbackQuery, session: AsyncSession) -> None:
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    result = await svc.wake(pet)
    await PetRepository(session).log_action(pet.id, "wake")
    await _after_action(cb, session, result, fx="wake")

@router.callback_query(F.data == "pet:wash")
async def act_wash(cb: CallbackQuery, session: AsyncSession) -> None:
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    # Страж состояний: во сне не купают, гуляющего негде мыть.
    deny = svc.state_deny(pet, "wash")
    if deny:
        return await cb.answer(deny, show_alert=True)
    result = await svc.wash(pet)
    await PetRepository(session).log_action(pet.id, "wash")
    await _after_action(cb, session, result, fx="wash")

@router.callback_query(F.data == "pet:train")
async def train_screen(cb: CallbackQuery, session: AsyncSession) -> None:
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    svc = TamagotchiService(session)
    # Экран тренировок недоступен, пока питомец спит или гуляет.
    deny = svc.state_deny(pet, "train")
    if deny:
        set_pet_page(cb.message.chat.id, 0)
        await safe_edit_or_answer(
            cb.message,
            await svc.render_async(pet),
            reply_markup=pet_hub(0, critical=svc.is_critical(pet),
                                 sleeping=pet.is_sleeping,
                                 walking=svc.on_walk(pet)),
        )
        return await cb.answer(deny, show_alert=True)
    set_pet_page(cb.message.chat.id, 0)
    sp = SPECIES_DATA.get(_species_key(pet), SPECIES_DATA["cat"])
    lines = [
        f"🏋️ <b>Тренировки {pet.name}</b>\n",
        f"💪 Сила {pet.strength} · 🏃 Ловкость {pet.agility} · 🧠 Интеллект {pet.intellect}\n",
        "Профильная тренировка твоего вида даёт +1 к приросту:",
        f"  💪 — профиль 🐶 · 🏃 — профиль 🦊 · 🧠 — профиль 🦉 (сейчас у тебя {sp['emoji']})\n",
        "⚡ Тренировка стоит 15 энергии и 8 сытости.",
    ]
    await safe_edit_or_answer(cb.message, "\n".join(lines),
                              reply_markup=train_menu(
                                  cb.message.chat.id if cb.message else None))
    await cb.answer()

@router.callback_query(F.data.startswith("pet:train:"))
async def act_train(cb: CallbackQuery, session: AsyncSession) -> None:
    stat = cb.data.split(":")[-1]
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    result = await svc.train(pet, stat)
    trained = "завершена" in result
    if trained:
        await PetRepository(session).log_action(pet.id, "train", value=1)
    await _after_action(cb, session, result, fx="train" if trained else None, stat=stat)

@router.callback_query(F.data == "pet:walk")
async def act_walk(cb: CallbackQuery, session: AsyncSession) -> None:
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    forecast = await walk_forecast_line()
    result = await svc.start_walk(pet, hours=2)
    walked = "ушёл гулять" in result
    if walked:
        await PetRepository(session).log_action(pet.id, "walk")
        if forecast:
            hint = weather_hint_block(walk=True, pet=pet)
            result = f"{result}\n{forecast}"
            if hint and hint.split("\n", 1)[0] not in forecast:
                result = f"{result}\n{hint}"
    elif forecast and ("🌧️" in forecast or "❄️" in forecast):
        result = f"{result}\n💡 Совет: {forecast.split('—', 1)[-1].strip()}"
    await _after_action(cb, session, result, fx="walk" if walked else None)

@router.callback_query(F.data == "pet:end_walk")
async def act_end_walk(cb: CallbackQuery, session: AsyncSession) -> None:
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    result = await svc.end_walk(pet)
    returned = not svc.on_walk(pet)
    if returned:
        await PetRepository(session).log_action(pet.id, "walk_done")
    await _after_action(cb, session, result, fx="wake" if returned else None)
