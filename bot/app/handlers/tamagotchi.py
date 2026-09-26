"""Хаб тамагочи: экран питомца + действия через callback-кнопки.

UX: все действия редактируют ОДНО сообщение (карточку питомца) — чтобы не
засорять ЛС. Уведомления об эволюции/ачивках — отдельными сообщениями.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pet
from app.db.repositories import PetRepository, UserRepository
from app.i18n import t
from app.keyboards.inline import (PET_PAGES, adopt_cta_kb, back_to_main,
                                   pet_history_kb, pet_hub,
                                   pet_page_count, train_menu)
from app.utils.safe_edit import safe_edit_or_answer
from app.utils.html_text import esc
from app.services.tamagotchi import (SPECIES_DATA, TamagotchiService, _aware,
                                     _species_key)
from app.utils.local_time import now as local_now

router = Router(name="tamagotchi")
_aware_dt = _aware  # алиас: walk_until из БД может быть naive (SQLite) — нормализуем

class AdoptConfirm(StatesGroup):
    """Двухшаговое подтверждение «усыновить нового» (v1.5.22).

    Раньше контекст жил в модульном dict (_ADOPT_CTX): текли память без TTL,
    терялся при рестарте и ломался при нескольких воркерах. FSM-стейт хранится
    в Redis (в dev — в памяти процесса) и очищается вместе с диалогом.
    """
    confirm = State()


async def _get_pet(session: AsyncSession, tg_id: int) -> Pet | None:
    return await PetRepository(session).get_by_user(tg_id)


# Страница хаба питомца, с которой пользователь пришёл на подэкран:
# кнопки «🏠 Меню» / «⬅️ Назад» возвращают туда, откуда пришли,
# а не на «нулевую» страницу. Запоминается per-chat.

_PET_PAGE_CTX: dict[int, int] = {}


def pet_page_for(chat_id: int) -> int:
    """Последняя открытая страница хаба для этого чата (0 по умолчанию)."""
    return _PET_PAGE_CTX.get(int(chat_id), 0) % max(1, pet_page_count())


def set_pet_page(chat_id: int, page: int) -> int:
    page %= max(1, pet_page_count())
    _PET_PAGE_CTX[int(chat_id)] = page
    return page


def _collect_walk_result(svc: TamagotchiService, pet: Pet, session: AsyncSession):
    """Если срок прогулки истёк — возвращаем текст события и начисления.

    ВАЖНО: walk_until снимает только этот хендлер (не apply_decay) — иначе
    фоновый тик scheduler'а «съедал» флаг раньше пользователя, и награды за
    прогулку терялись молча.
    """
    if pet.walk_until is None:
        return None
    if local_now() < _aware_dt(pet.walk_until):
        return None   # ещё гуляет
    text, coins, xp = svc.finish_walk_event(pet)
    return text, coins, xp


HELP_TEXT = (
    "🐾 <b>Как я работаю</b>\n\n"
    "<b>Основное</b>\n"
    "/start — главное меню и онбординг (создание питомца)\n"
    "/pet — карточка твоего питомца\n"
    "/stats — твоя статистика (XP, уровень, монеты)\n"
    "/ach — достижения\n"
    "/top — топы чата\n"
    "/card — PNG-карточка профиля\n"
    "/award — итоги недели\n"
    "/settings — настройки уведомлений\n"
    "/help — эта справка\n\n"
    "<b>Заработок XP и 🪙</b>\n"
    "• Сообщения в чате (текст, фото, голосовые, кружки, стикеры) — с кулдауном;\n"
    "• Реакции на сообщения собеседника (с дневным лимитом антифрода);\n"
    "• Активность в канале, если бот там админ.\n\n"
    "<b>Питомец</b>\n"
    "Корми, мой, играй, тренируй и гуляй — за активность капают монеты,\n"
    "а питомец растёт и эволюционирует. Не забывай: без ухода он скучает!\n"
)


@router.message(Command("help"), F.chat.type == "private")
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="HTML")


@router.message(Command("pet"), F.chat.type == "private")
async def cmd_pet(message: Message, session: AsyncSession) -> None:
    """Текстовый дубликат кнопки «🐾 Питомец» (команда есть в меню Telegram)."""
    svc = TamagotchiService(session)
    pet = await _get_pet(session, message.from_user.id)
    if pet is None:
        await message.answer("🥚 У тебя пока нет питомца. Нажми /start и пройди онбординг!")
        return
    await svc.apply_decay(pet)
    users = UserRepository(session)
    user = await users.get(message.from_user.id)
    await message.answer(await svc.render_async(pet, user.first_name if user else ""),
                         reply_markup=pet_hub(pet_page_for(message.chat.id),
                                              sleeping=pet.is_sleeping),
                         parse_mode="HTML")


@router.callback_query(F.data == "menu:pet")
async def pet_screen(cb: CallbackQuery, session: AsyncSession) -> None:
    """Открыть хаб питомца на последней посещённой странице."""
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        await safe_edit_or_answer(cb.message, 
            "🥚 У тебя пока нет питомца. Нажми /start и пройди онбординг!",
            reply_markup=back_to_main(),
        )
        await cb.answer()
        return
    await svc.apply_decay(pet)
    users = UserRepository(session)
    user = await users.get(cb.from_user.id)
    text = await svc.render_async(pet, user.first_name if user else "")
    await safe_edit_or_answer(cb.message, text,
                              reply_markup=pet_hub(pet_page_for(cb.message.chat.id),
                                                   sleeping=pet.is_sleeping))
    await cb.answer()


@router.callback_query(F.data == "pet:noop")
async def pet_hub_noop(cb: CallbackQuery) -> None:
    """Клик по неразрывной подписи страницы хаба — просто снять «часики»."""
    await cb.answer()


@router.callback_query(F.data == "pet:page:0")
@router.callback_query(F.data.startswith("pet:page:"))
async def pet_page_screen(cb: CallbackQuery, session: AsyncSession) -> None:
    """Навигация ◀️/▶️ по страницам хаба (Уход → Вещи → Досуг)."""
    try:
        page = int(cb.data.split(":")[-1])
    except ValueError:
        page = 0
    page = set_pet_page(cb.message.chat.id, page)
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        # питомец — опция. Без него показываем не «ошибку», а экран
        # с приглашением завести (или просто вернуться в меню).
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
        reply_markup=pet_hub(page, critical=crit, sleeping=pet.is_sleeping),
    )
    await cb.answer()


@router.callback_query(F.data == "pet:revive")
async def act_revive(cb: CallbackQuery, session: AsyncSession) -> None:
    """💖 Реанимация (v1.4.7): платная, цена растёт 200→400→600, максимум 3 раза.
    Новичку без монет первая реанимация — бесплатно (one-shot), чтобы смерть
    на первой неделе не убивала мотивацию."""
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    await svc.apply_decay(pet)
    if not svc.is_critical(pet):
        await safe_edit_or_answer(
            cb.message, await svc.render_async(pet),
            reply_markup=pet_hub(pet_page_for(cb.message.chat.id),
                                 sleeping=pet.is_sleeping))
        return await cb.answer(t("pet.not_critical"), show_alert=True)
    users = UserRepository(session)
    user = await users.get(cb.from_user.id)
    cost = svc.revive_cost(pet)
    if cost < 0:
        # жизни кончились: сразу предлагаем усыновление
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


@router.callback_query(F.data == "pet:adopt", AdoptConfirm.confirm)
async def pet_adopt_confirm(cb: CallbackQuery, session: AsyncSession,
                            state: FSMContext) -> None:
    """Подтверждение усыновления (2-й клик): архивируем текущего, открываем пикер."""
    svc = TamagotchiService(session)
    data = await state.get_data()
    await state.clear()
    repo = PetRepository(session)
    current = await repo.get_by_user(cb.from_user.id)
    if current and current.id == data.get("pet_id"):
        await svc.archive_pet(session, current, reason="rehomed")
        await session.commit()
    from app.keyboards.inline import species_picker
    await safe_edit_or_answer(
        cb.message,
        "🐣 Прежний питомец пристроен в историю. Выбери нового:\n\n"
        + _species_picker_text(),
        reply_markup=species_picker())
    await cb.answer()


@router.callback_query(F.data == "pet:adopt")
async def pet_adopt_screen(cb: CallbackQuery, session: AsyncSession,
                           state: FSMContext) -> None:
    """🥚 «Усыновить нового»: архивируем текущего (с подтверждением через
    повторное нажатие) и открываем пикер вида."""
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        # нет текущего — просто заводим с нуля (тот же флоу, что онбординг)
        from app.keyboards.inline import species_picker
        return await safe_edit_or_answer(
            cb.message,
            "🐣 Выбери питомца — у каждого свой характер и бонусы:\n\n"
            + _species_picker_text(),
            reply_markup=species_picker())
    await state.set_state(AdoptConfirm.confirm)
    await state.update_data(pet_id=pet.id)
    await safe_edit_or_answer(
        cb.message,
        f"⚠️ Ты уверен, что хочешь усыновить нового питомца?\n\n"
        f"Текущий — <b>{esc(pet.name)}</b> (ур. {pet.level}, поколении "
        f"{pet.generation}) — уйдёт в историю 📜: его уровень, ачивки и логи "
        "сохранятся, но прогресс не перенесётся.\n\n"
        "Нажми ещё раз для подтверждения или вернись назад.",
        reply_markup=back_to_main())
    await cb.answer()


def _species_picker_text() -> str:
    from app.services.tamagotchi import SPECIES_DATA
    lines = []
    for code, sp in SPECIES_DATA.items():
        bonus = ", ".join(f"{k}: {v}" for k, v in sp.get("bonus", {}).items()) \
            if sp.get("bonus") else "без особых бонусов"
        lines.append(f"{sp['emoji']} <b>{sp['title']}</b> — {bonus}")
    return "\n".join(lines)


@router.callback_query(F.data == "pet:history")
async def pet_history_screen(cb: CallbackQuery, session: AsyncSession) -> None:
    """📜 Экран истории питомцев (v1.4.7): все архивные поколения владельца."""
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
                              reply_markup=pet_history_kb(has_current=current is not None))
    await cb.answer()


async def _after_action(cb: CallbackQuery, session: AsyncSession, result_text: str) -> None:
    """Единый постобработчик: перерендер карточки + лог действия.

    Если прогулка завершилась (walk_until ещё висит, но срок истёк),
    добираем её награды и только затем снимаем флаг.
    """
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
                                 sleeping=pet.is_sleeping),
        )
    finally:
        # ВАЖНО: коммит в finally — Telegram-редактирование не откатить, а без
        # явного commit'а при сетевом исключении сессия откатится в middleware:
        # юзер увидел бы награду/новые статы, которых нет в БД (рассинхрон UI).
        await session.commit()
    await cb.answer()


@router.callback_query(F.data == "pet:feed")
async def act_feed(cb: CallbackQuery, session: AsyncSession) -> None:
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    # бесплатная «базовая еда» (хлеб); вкусняшки — из инвентаря (магазин)
    result = await svc.feed(pet, {"hunger": 15})
    if "Ням-ням" in result:  # не считать кормлением попытки «на кулдауне»
        await PetRepository(session).log_action(pet.id, "feed")
    await _after_action(cb, session, result)


@router.callback_query(F.data == "pet:play")
async def act_play(cb: CallbackQuery, session: AsyncSession) -> None:
    """MVP-игра «угадай число» упрощена до честного рандома с бонусом ловкости.

    На этапе 3.5 заменим на полноценную мини-игру с FSM (ввод числа / RPS).
    """
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    win_chance = 0.45 + pet.agility * 0.01  # тренировки реально повышают шанс
    won = random.random() < min(win_chance, 0.85)
    result = await svc.play(pet, won)
    await PetRepository(session).log_action(pet.id, "play", value=int(won))
    await _after_action(cb, session, result)


@router.callback_query(F.data == "pet:sleep")
async def act_sleep(cb: CallbackQuery, session: AsyncSession) -> None:
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    if pet.is_sleeping:
        # во сне та же кнопка уже «Разбудить» (см. pet_hub(sleeping=...));
        # повторный клик по старой кнопке — просто разбудить.
        result = await svc.wake(pet)
    else:
        result = await svc.sleep(pet, hours=8)
    await PetRepository(session).log_action(pet.id, "sleep")
    await _after_action(cb, session, result)


@router.callback_query(F.data == "pet:wake")
async def act_wake(cb: CallbackQuery, session: AsyncSession) -> None:
    """Принудительно разбудить: накопленная за сон энергия сохраняется."""
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    result = await svc.wake(pet)
    await PetRepository(session).log_action(pet.id, "wake")
    await _after_action(cb, session, result)


@router.callback_query(F.data == "pet:wash")
async def act_wash(cb: CallbackQuery, session: AsyncSession) -> None:
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    result = await svc.wash(pet)
    await PetRepository(session).log_action(pet.id, "wash")
    await _after_action(cb, session, result)


@router.callback_query(F.data == "pet:train")
async def train_screen(cb: CallbackQuery, session: AsyncSession) -> None:
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    set_pet_page(cb.message.chat.id, 0)  # тренировки — страница «Уход»
    sp = SPECIES_DATA.get(_species_key(pet), SPECIES_DATA["cat"])
    lines = [
        f"🏋️ <b>Тренировки {pet.name}</b>\n",
        f"💪 Сила {pet.strength} · 🏃 Ловкость {pet.agility} · 🧠 Интеллект {pet.intellect}\n",
        "Профильная тренировка твоего вида даёт +1 к приросту:",
        f"  💪 — профиль 🐶 · 🏃 — профиль 🦊 · 🧠 — профиль 🦉 (сейчас у тебя {sp['emoji']})\n",
        "⚡ Тренировка стоит 15 энергии и 8 сытости.",
    ]
    await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=train_menu())
    await cb.answer()


@router.callback_query(F.data.startswith("pet:train:"))
async def act_train(cb: CallbackQuery, session: AsyncSession) -> None:
    stat = cb.data.split(":")[-1]
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    result = await svc.train(pet, stat)
    if "завершена" in result:
        await PetRepository(session).log_action(pet.id, "train", value=1)
    await _after_action(cb, session, result)


@router.callback_query(F.data == "pet:walk")
async def act_walk(cb: CallbackQuery, session: AsyncSession) -> None:
    svc = TamagotchiService(session)
    pet = await _get_pet(session, cb.from_user.id)
    if pet is None:
        return await cb.answer()
    result = await svc.start_walk(pet, hours=2)
    await PetRepository(session).log_action(pet.id, "walk")
    await _after_action(cb, session, result)



