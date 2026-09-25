"""Хаб тамагочи: экран питомца + действия через callback-кнопки.

UX: все действия редактируют ОДНО сообщение (карточку питомца) — чтобы не
засорять ЛС. Уведомления об эволюции/ачивках — отдельными сообщениями.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.db.models import Pet
from app.db.repositories import PetRepository, UserRepository
from app.keyboards.inline import back_to_main, pet_hub, train_menu
from app.utils.safe_edit import safe_edit_or_answer
from app.services.tamagotchi import (SPECIES_DATA, TamagotchiService, _aware,
                                     _species_key)

router = Router(name="tamagotchi")
_aware_dt = _aware  # алиас: walk_until из БД может быть naive (SQLite) — нормализуем


async def _get_pet(session: AsyncSession, tg_id: int) -> Pet | None:
    return await PetRepository(session).get_by_user(tg_id)


def _collect_walk_result(svc: TamagotchiService, pet: Pet, session: AsyncSession):
    """Если срок прогулки истёк — возвращаем текст события и начисления.

    ВАЖНО (регресс v1.4.5): раньше walk_until снимал apply_decay, и фоновый
    тик scheduler'а (каждые 30 мин) «съедал» флаг раньше пользователя —
    награды за прогулку терялись молча. Теперь флаг доживает до хендлера.
    """
    if pet.walk_until is None:
        return None
    if datetime.now(timezone.utc) < _aware_dt(pet.walk_until):
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


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="HTML")


@router.message(Command("pet"))
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
    await message.answer(svc.render(pet, user.first_name if user else ""),
                         reply_markup=pet_hub(), parse_mode="HTML")


@router.callback_query(F.data == "menu:pet")
async def pet_screen(cb: CallbackQuery, session: AsyncSession) -> None:
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
    text = svc.render(pet, user.first_name if user else "")
    await safe_edit_or_answer(cb.message, text, reply_markup=pet_hub())
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
            f"{prefix}{result_text}\n\n" + svc.render(pet),
            reply_markup=pet_hub(),
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
        # кнопка работает и как «разбудить»
        pet.is_sleeping = False
        pet.sleep_until = None
        result = "⏰ Ты разбудил питомца."
    else:
        result = await svc.sleep(pet, hours=8)
    await PetRepository(session).log_action(pet.id, "sleep")
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


# Экраны «🛒 Магазин» (pet:shop) и «🎒 Инвентарь» (pet:inv) ранее были
# заглушками «магазин откроется позже». Этап 4 реализован в app/handlers/shop.py
# (магазин, покупка за монеты, инвентарь, использование предметов), причём
# shop.router регистрируется ПОСЛЕ tamagotchi.router — из-за чего эти мёртвые
# заглушки перехватывали колбэки первыми и пользователь никогда не видел
# настоящий магазин. Заглушки удалены; маршрутизация делегирована в shop.py.
