"""Магазин и инвентарь (Этап 4, ранняя версия).

Экономика: монеты капают за активность (1/сообщение с кулдауном) и прогулки.
Цены подобраны так, чтобы «базовая еда» была доступна почти сразу, а вкусняшки —
целью на несколько дней. Покупки идут в pet_inventory; кормление из инвентаря
использует реальные эффекты предметов.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.db.models import Item, PetInventory
from app.db.repositories import PetRepository, UserRepository
from app.keyboards.inline import back_to_main, pet_hub
from app.services.tamagotchi import TamagotchiService
from app.utils.safe_edit import safe_edit_or_answer

router = Router(name="shop")


ITEMS_SEED = [
    dict(code="food_bread", name="Хлеб", icon="🍞", type="food", price=5,
         effect={"hunger": 15}, description="Просто, дёшево, сердито."),
    dict(code="food_apple", name="Яблоко", icon="🍎", type="food", price=10,
         effect={"hunger": 25, "health": 3}, description="Полезно!"),
    dict(code="food_meat", name="Стейк", icon="🥩", type="food", price=25,
         effect={"hunger": 45, "strength": 1}, description="Сытно и для силы."),
    dict(code="food_cake", name="Тортик", icon="🍰", type="food", price=40,
         effect={"hunger": 30, "happiness": 15, "hygiene": -5},
         description="Вкусно, но потом мыться!"),
    dict(code="toy_ball", name="Мячик", icon="⚽", type="toy", price=30,
         effect={"happiness": 10}, description="Игрушка: играет сам, чуть поднимает счастье."),
    dict(code="toy_laser", name="Лазерная указка", icon="🔦", type="toy", price=80,
         effect={"happiness": 20, "agility": 1}, description="Кошачий экстаз."),
    dict(code="med_pill", name="Лекарство", icon="💊", type="medicine", price=35,
         effect={"health": 35}, description="Лечит болезни."),
    dict(code="med_vitamins", name="Витамины", icon="🧪", type="medicine", price=60,
         effect={"health": 15, "energy": 15}, description="Бодрость и здоровье."),
]


async def seed_items(session: AsyncSession) -> int:
    existing = {r for r in (await session.execute(select(Item.code))).scalars()}
    created = 0
    for i, spec in enumerate(ITEMS_SEED, start=1):
        if spec["code"] in existing:
            continue
        session.add(Item(id=i, **spec))
        created += 1
    if created:
        await session.flush()
    return created


def shop_keyboard(items: list[Item], user_coins: int) -> "InlineKeyboardBuilder | None":
    b = InlineKeyboardBuilder()
    for it in items:
        afford = "🪙" if user_coins >= it.price else "🔒"
        b.button(text=f"{afford} {it.icon} {it.name} · {it.price}", callback_data=f"buy:{it.id}")
    b.adjust(1)
    return b


@router.callback_query(F.data == "pet:shop")
async def shop_screen(cb: CallbackQuery, session: AsyncSession) -> None:
    users = UserRepository(session)
    user = await users.get(cb.from_user.id)
    if user is None:
        return await cb.answer()
    items = list((await session.execute(select(Item).order_by(Item.type, Item.price))).scalars())
    if not items:
        await seed_items(session)
        items = list((await session.execute(select(Item).order_by(Item.type, Item.price))).scalars())
    lines = [f"🛒 <b>Магазин</b> · у тебя 🪙 {user.coins}\n"]
    groups: dict[str, list[Item]] = {}
    for it in items:
        groups.setdefault(it.type, []).append(it)
    titles = {"food": "🍎 Еда", "toy": "🎾 Игрушки", "medicine": "💊 Лекарства"}
    for t, lst in groups.items():
        lines.append(f"<b>{titles.get(t, t)}</b>")
        for it in lst:
            lines.append(f"  {it.icon} {it.name} — {it.price} 🪙 · {it.description}")
        lines.append("")
    kb = shop_keyboard(items, user.coins)
    kb.button(text="⬅️ Назад", callback_data="menu:pet")
    await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("buy:"))
async def buy_item(cb: CallbackQuery, session: AsyncSession) -> None:
    item_id = int(cb.data.split(":")[1])
    users = UserRepository(session)
    pets = PetRepository(session)
    user = await users.get(cb.from_user.id)
    pet = await pets.get_by_user(cb.from_user.id)
    if user is None or pet is None:
        return await cb.answer("Нет питомца или пользователя", show_alert=True)
    item = await session.get(Item, item_id)
    if item is None:
        return await cb.answer("Предмет не найден", show_alert=True)
    if user.coins < item.price:
        await cb.answer(f"Не хватает {item.price - user.coins} монет 🪙", show_alert=True)
        return

    user.coins -= item.price
    inv_row = (await session.execute(
        select(PetInventory).where(PetInventory.pet_id == pet.id,
                                   PetInventory.item_id == item.id)
    )).scalar_one_or_none()
    if inv_row:
        inv_row.quantity += 1
    else:
        session.add(PetInventory(pet_id=pet.id, item_id=item.id, quantity=1))
    await session.flush()
    await pets.log_action(pet.id, "buy", value=item.price, meta={"item": item.code})
    logger.info("user {} bought {} for {}", user.tg_id, item.code, item.price)
    await cb.answer(f"Куплено: {item.icon} {item.name}!", show_alert=False)
    # перерендерим магазин, чтобы цены-замки обновились
    await shop_screen(cb, session)


@router.callback_query(F.data == "pet:inv")
async def inventory_screen(cb: CallbackQuery, session: AsyncSession) -> None:
    pets = PetRepository(session)
    pet = await pets.get_by_user(cb.from_user.id)
    if pet is None:
        return await cb.answer()
    rows = (await session.execute(
        select(PetInventory, Item).join(Item, Item.id == PetInventory.item_id)
        .where(PetInventory.pet_id == pet.id)
    )).all()
    if not rows:
        await safe_edit_or_answer(cb.message, 
            "🎒 Инвентарь пуст. Загляни в 🛒 Магазин!",
            reply_markup=pet_hub(),
        )
        await cb.answer()
        return

    b = InlineKeyboardBuilder()
    lines = ["🎒 <b>Инвентарь</b>\n"]
    for inv, item in rows:
        lines.append(f"{item.icon} {item.name} ×{inv.quantity}")
        b.button(text=f"Использовать {item.icon}×{inv.quantity}",
                 callback_data=f"use:{item.id}")
    b.adjust(1)
    b.button(text="⬅️ Назад", callback_data="menu:pet")
    await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=b.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("use:"))
async def use_item(cb: CallbackQuery, session: AsyncSession) -> None:
    item_id = int(cb.data.split(":")[1])
    pets = PetRepository(session)
    pet = await pets.get_by_user(cb.from_user.id)
    if pet is None:
        return await cb.answer()
    inv = (await session.execute(
        select(PetInventory).where(PetInventory.pet_id == pet.id,
                                   PetInventory.item_id == item_id)
    )).scalar_one_or_none()
    if inv is None or inv.quantity <= 0:
        return await cb.answer("Предмета нет в инвентаре", show_alert=True)
    item = await session.get(Item, item_id)

    svc = TamagotchiService(session)
    if item.type == "food":
        result = await svc.feed(pet, {k: v for k, v in item.effect.items()})
    elif item.type == "medicine":
        if item.code == "med_pill":
            result = await svc.heal(pet)
        else:
            for stat, delta in item.effect.items():
                from app.utils.formatting import clamp
                setattr(pet, stat, clamp(getattr(pet, stat) + delta))
            result = f"{item.icon} {item.name} применён!"
    elif item.type == "toy":
        result = await svc.play(pet, won=False)  # игрушка = пассивная игра без проигрыша
    else:
        result = "❓ Этот предмет пока нельзя использовать."

    inv.quantity -= 1
    if inv.quantity <= 0:
        await session.delete(inv)
    await session.flush()
    await pets.log_action(pet.id, "use", meta={"item": item.code})
    await safe_edit_or_answer(cb.message, f"{result}\n\n" + svc.render(pet), reply_markup=pet_hub())
    await cb.answer()
