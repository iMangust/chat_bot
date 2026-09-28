"""Магазин и инвентарь.

Экономика: монеты капают за активность (1/сообщение с кулдауном) и прогулки.
Цены подобраны так, чтобы «базовая еда» была доступна почти сразу, а вкусняшки —
целью на несколько дней. Покупки идут в pet_inventory; кормление из инвентаря
использует реальные эффекты предметов.

🧢 Мерч канала живёт в отдельном разделе (app/handlers/merch.py) — он про канал,
а не про питомца. Здесь мерча нет; в магазине оставлена только кнопка-переход.
"""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from aiogram.types import InlineKeyboardButton

from app.db.models import Item, PetInventory, User
from app.i18n import t
from app.db.repositories import PetRepository, UserRepository
from app.keyboards.inline import pet_hub
from app.keyboards.paged import paged_keyboard, paged_pages
from app.handlers.tamagotchi import set_pet_page
from app.services.tamagotchi import TamagotchiService
from app.utils.safe_edit import safe_edit_or_answer

router = Router(name="shop")

# Страница магазина per-chat: cb.data frozen (v1.5.18), поэтому после покупки
# перерендер возвращает юзера на ту же страницу, где он купил товар, а не
# сбрасывает на первую (баг v1.5.24: «купил — улетел на стр. 1»).
_SHOP_PAGE_CTX: dict[int, int] = {}


ITEMS_SEED = [
    # ---------------- 🍞 Еда (быстрое насыщение) --------------------------
    dict(code="food_bread", name="Хлеб", icon="🍞", type="food", price=5,
         effect={"hunger": 15}, description="Просто, дёшево, сердито."),
    dict(code="food_apple", name="Яблоко", icon="🍎", type="food", price=10,
         effect={"hunger": 25, "health": 3}, description="Полезно!"),
    dict(code="food_carrot", name="Морковка", icon="🥕", type="food", price=8,
         effect={"hunger": 18, "hygiene": 3}, description="Сытно и зубам хорошо."),
    dict(code="food_fish", name="Рыбка", icon="🐟", type="food", price=18,
         effect={"hunger": 32, "strength": 1}, description="Камчатский улов: сила +1."),
    dict(code="food_meat", name="Стейк", icon="🥩", type="food", price=25,
         effect={"hunger": 45, "strength": 1}, description="Сытно и для силы."),
    dict(code="food_soup", name="Горячий суп", icon="🍲", type="food", price=20,
         effect={"hunger": 38, "health": 5}, description="Согревает и лечит чуть-чуть."),
    dict(code="food_cake", name="Тортик", icon="🍰", type="food", price=40,
         effect={"hunger": 30, "happiness": 15, "hygiene": -5},
         description="Вкусно, но потом мыться!"),
    dict(code="food_donut", name="Пончик", icon="🍩", type="food", price=15,
         effect={"hunger": 18, "happiness": 8}, description="Сахарное счастье."),
    dict(code="food_sushi", name="Сендвич сёмги", icon="🍣", type="food", price=45,
         effect={"hunger": 42, "happiness": 8, "intellect": 1},
         description="Дальневосточный деликатес: ум +1."),
    # ---------------- 🎯 Ужин с баффом (съел → временный эффект) ----------
    dict(code="food_feast", name="Праздничный ужин", icon="🦞", type="food", price=70,
         effect={"hunger": 60, "happiness": 10,
                 "buff": {"type": "food_feast", "mult": 0.25, "duration": 1800,
                          "label": "Сытный час"}},
         description="+60 сытости и 30 мин еда усваивается на +25%."),
    dict(code="food_honey", name="Бочонок мёда", icon="🍯", type="food", price=55,
         effect={"hunger": 30, "health": 8,
                 "buff": {"type": "happy_pct", "mult": 0.20, "duration": 3600,
                          "label": "Медовое настроение"}},
         description="Здоровье +8 и час игры дают +20% счастья."),
    # ---------------- 🥤 Напитки (бодрость и временные бафы) --------------
    dict(code="drink_water", name="Водичка", icon="💧", type="drink", price=4,
         effect={"energy": 5, "hygiene": -2}, description="Просто попить."),
    dict(code="drink_juice", name="Сок", icon="🧃", type="drink", price=10,
         effect={"energy": 10, "happiness": 3}, description="Витаминный заряд."),
    dict(code="drink_milk", name="Молоко", icon="🥛", type="drink", price=12,
         effect={"energy": 12, "health": 3, "hunger": 5}, description="Крепкие кости."),
    dict(code="drink_coffee", name="Кофе", icon="☕", type="drink", price=20,
         effect={"energy": 20,
                 "buff": {"type": "no_decay", "mult": 0.5, "duration": 1800,
                          "label": "Кофеиновый щит"}},
         description="⚡+20 и 30 мин энергия тратится вдвое медленнее."),
    dict(code="drink_energy", name="Энергетик", icon="⚡", type="drink", price=35,
         effect={"energy": 40, "happiness": -3,
                 "buffs": [
                     {"type": "energy_regen_pct", "mult": 1.0, "duration": 3600,
                      "label": "Передоз бодрости"},
                     {"type": "train_pct", "mult": 0.5, "duration": 3600,
                      "label": "Предтрен"},
                 ]},
         description="⚡+40; час сон восстанавливает ×2 ⚡, тренировки +50%."),
    dict(code="drink_tea", name="Иван-чай", icon="🍵", type="drink", price=15,
         effect={"energy": 8, "health": 4}, description="Камчатский травяной, бодрит мягко."),
    dict(code="drink_smoothie", name="Смузи из ягод", icon="🫐", type="drink", price=25,
         effect={"energy": 15, "happiness": 6,
                 "buff": {"type": "xp_pct", "mult": 0.15, "duration": 3600,
                          "label": "Ягодная ясность"}},
         description="⚡+15 и час все дела дают +15% XP."),
    # ---------------- 🎾 Игрушки -------------------------------------------
    dict(code="toy_ball", name="Мячик", icon="⚽", type="toy", price=30,
         effect={"happiness": 10}, description="Игрушка: играет сам, чуть поднимает счастье."),
    dict(code="toy_laser", name="Лазерная указка", icon="🔦", type="toy", price=80,
         effect={"happiness": 20, "agility": 1}, description="Кошачий экстаз."),
    dict(code="toy_puzzle", name="Головоломка", icon="🧩", type="toy", price=60,
         effect={"happiness": 12, "intellect": 1}, description="Ум растёт, лапы не устают."),
    # ---------------- 💊 Лекарства -----------------------------------------
    dict(code="med_pill", name="Лекарство", icon="💊", type="medicine", price=35,
         effect={"health": 35}, description="Лечит болезни."),
    dict(code="med_vitamins", name="Витамины", icon="🧪", type="medicine", price=60,
         effect={"health": 15, "energy": 15}, description="Бодрость и здоровье."),
    dict(code="med_syrup", name="Сироп от кашля", icon="🍯", type="medicine", price=45,
         effect={"health": 25}, description="Мягкое лечение, быстрее ставит на лапы."),
]

# Мерч не хранится в таблице Items: он вынесен в отдельный раздел
# 🧢 Мерч канала (app/handlers/merch.py) и живёт из конфига/дефолтной витрины.


async def seed_items(session: AsyncSession) -> int:
    existing = {r for r in (await session.execute(select(Item.code))).scalars()}
    created = 0
    next_id = ((await session.execute(select(Item.id).order_by(Item.id.desc()).limit(1)))
               .scalars().first() or 0)
    for spec in ITEMS_SEED:
        if spec["code"] in existing:
            continue
        next_id += 1
        session.add(Item(id=next_id, **spec))
        created += 1
    if created:
        await session.flush()
    return created


def shop_keyboard(items: list[Item], user_coins: int) -> "InlineKeyboardBuilder | None":
    b = InlineKeyboardBuilder()
    for it in items:
        if it.type == "merch":
            continue  # старый мерч в БД игнорируем — он в отдельном разделе
        afford = "🪙" if user_coins >= it.price else "🔒"
        b.button(text=f"{afford} {it.icon} {html.escape(it.name)} · {it.price}",
                 callback_data=f"buy:{it.id}")
    b.adjust(1)
    return b


@router.callback_query(F.data == "pet:shop")
@router.callback_query(F.data.startswith("shop:page:"))
@router.callback_query(F.data.startswith("shop:back"))   # совместимость со старыми клавиатурами
@router.callback_query(F.data.startswith("shop:next"))   # в сообщениях пользователей (баг v1.5.48)
async def shop_screen(cb: CallbackQuery, session: AsyncSession,
                      page: int | None = None) -> None:
    """Экран магазина. page — явный номер страницы (используется после
    покупки: cb.data менять нельзя — объект frozen, v1.5.18).

    Листание: ◀️/▶️ генерируются как ``shop:page:<n>`` (абсолютный индекс),
    но старые инстансы сообщений могли содержать ``shop:back`` / ``shop:next``
    (относительные) — они обрабатываются здесь же, иначе клик по ним не имеет
    обработчика и кнопка «не работает» (жалоба пользователя v1.5.48)."""
    set_pet_page(cb.message.chat.id, 1)  # «Назад» из магазина вернёт на стр. «Вещи»
    users = UserRepository(session)
    user = await users.get(cb.from_user.id)
    if user is None:
        return await cb.answer()
    items = list((await session.execute(
        select(Item).where(Item.type != "merch").order_by(Item.type, Item.price)
    )).scalars())
    if not items:
        await seed_items(session)
        items = list((await session.execute(
            select(Item).where(Item.type != "merch").order_by(Item.type, Item.price)
        )).scalars())
    # магазин листается постранично (≤6 товаров на страницу), текст показывает
    # только товары текущей страницы — кнопки и список всегда синхронны.
    # Страница: явный аргумент page (после покупки) либо из callback_data
    # (shop:page:<n>).
    if page is None:
        data = cb.data or ""
        try:
            if data.startswith("shop:page:"):
                page = int(data.split(":")[2])
            elif data.startswith("shop:back") or data.startswith("shop:next"):
                # относительное листание из старых инстансов клавиатуры —
                # отталкиваемся от запомненной страницы этого чата
                cur = _SHOP_PAGE_CTX.get(cb.message.chat.id, 0)
                page = cur - 1 if data.startswith("shop:back") else cur + 1
            else:
                page = 0
        except (IndexError, ValueError):
            page = 0
    else:
        # защита: page мог прийти строкой из callback-парсинга
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = 0
    grouped = [it for t in ("food", "drink", "toy", "medicine")
               for it in items if it.type == t] + \
              [it for it in items if it.type not in ("food", "drink", "toy", "medicine")]
    # ВАЖНО: страницы режет paged_pages — тот же алгоритм, что у кнопок.
    # Раньше текст резался по 6 «в ряд», а paged_keyboard раскладывал кнопки
    # по 2 в ряд и резал по-своему: счётчик показывал «стр. 1/4», а ◀️/▶️
    # уходили в clamp — листание магазина было невозможно (баг v1.5.24).
    # total_pages = len(paged_pages(...)) — пустые строки-заполнители тоже
    # учитываются, поэтому индекс строки grouped == индекс кнопки на странице.
    #
    # КЛЮЧЕВОЕ (v1.5.48): контентные кнопки строятся ДО подсчёта страниц и
    # передаются в paged_keyboard готовыми `pages`. Раньше кнопки строились
    # только из chunk текущей страницы, а paged_keyboard перерезал их ЗАНОВО:
    # при page=0 он видел 6 кнопок → total=1 → ◀️/▶️ не создавались вовсе
    # («нет возможности проматывать страницы» — жалоба пользователя).
    content_buttons = [
        InlineKeyboardButton(
            text=f"{'🪙' if user.coins >= it.price else '🔒'} {it.icon} {html.escape(it.name)} · {it.price}🪙",
            callback_data=f"buy:{it.id}")
        for it in grouped
    ]
    all_pages = paged_pages(content_buttons)
    total_pages = len(all_pages)
    page = max(0, min(page, total_pages - 1))
    chunk = grouped[page * 6:(page + 1) * 6]
    titles = {"food": "🍎 Еда", "drink": "🥤 Напитки", "toy": "🎾 Игрушки",
              "medicine": "💊 Лекарства"}
    lines = [f"🛒 <b>Магазин питомца</b> · у тебя 🪙 {user.coins}"
             + (f" · стр. {page + 1}/{total_pages}" if total_pages > 1 else ""), ""]
    last_type = None
    for it in chunk:
        head = titles.get(it.type, html.escape(it.type or ""))
        if it.type != last_type:
            lines.append(f"<b>{head}</b>")
            last_type = it.type
        price = f" — {it.price} 🪙" if it.price else ""
        lines.append(f"  {it.icon} {html.escape(it.name)}{price} · {html.escape(it.description or '')}")
    lines.append("")
    lines.append("<i>🪙 — по карману, 🔒 — не хватает монет</i>")

    # КЛЮЧЕВОЕ (v1.5.48): передаём ГОТОВЫЕ страницы всех товаров — иначе
    # paged_keyboard режет заново только chunk текущей страницы, видит 6
    # кнопок → total=1 → ◀️/▶️ не создавались вовсе (листание магазина было
    # невозможно — жалоба пользователя).
    kb, page = paged_keyboard(
        content_buttons, prefix="shop", title="🛒 Магазин", page=page,
        back_cb="menu:main", pages=all_pages,
    )
    _SHOP_PAGE_CTX[cb.message.chat.id] = page  # помним страницу для возврата после покупки
    await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.in_({"shop:noop", "inv:noop"}))
async def shop_noop(cb: CallbackQuery) -> None:
    """Клик по неразрывной подписи страницы — просто снять «часики»."""
    await cb.answer()


@router.callback_query(F.data.startswith("buy:"))
async def buy_item(cb: CallbackQuery, session: AsyncSession) -> None:
    try:
        item_id = int(cb.data.split(":")[1])
    except (IndexError, ValueError):
        return await cb.answer("Битая кнопка 😅", show_alert=True)
    users = UserRepository(session)
    pets = PetRepository(session)
    user = await users.get(cb.from_user.id)
    pet = await pets.get_by_user(cb.from_user.id)
    if user is None or pet is None:
        return await cb.answer("Нет питомца или пользователя", show_alert=True)
    item = await session.get(Item, item_id)
    if item is None:
        return await cb.answer("Предмет не найден", show_alert=True)
    if item.type == "merch":
        # мерч вынесен в отдельный раздел — показываем витрину, монеты не трогаем
        from app.handlers.merch import merch_screen
        await cb.answer()
        return await merch_screen(cb)
    if user.coins < item.price:
        await cb.answer(f"Не хватает {item.price - user.coins} монет 🪙", show_alert=True)
        return

    # v1.5.18: НЕ присваиваем cb.data — объекты aiogram frozen (pydantic),
    # мутация «после покупки» кидала ValidationError на каждом buy:/use:.
    # Страницу возврата передаём параметром в shop_screen.
    # защита от двойного списания при быстрых дабл-кликах: UPDATE ... WHERE coins>=price
    from sqlalchemy import update
    res = await session.execute(
        update(User).where(User.tg_id == user.tg_id, User.coins >= item.price)
        .values(coins=User.coins - item.price)
    )
    if res.rowcount == 0:
        return await cb.answer("Недостаточно монет 🪙", show_alert=True)
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
    await session.commit()   # фиксируем списание сразу (res.rowcount уже проверен)
    logger.info("user {} bought {} for {}", user.tg_id, item.code, item.price)
    await cb.answer(f"🛒 Куплено: {item.icon} {item.name}!", show_alert=False)
    # реакция-«покупка» на сообщении витрины (best-effort, см. app/utils/fx.py)
    # единый безопасный путь: типизированный SetMessageReaction через cb.bot
    from app.utils.fx import EFFECTS, react_to_message
    await react_to_message(cb, EFFECTS["coin"].primary[0])
    # перерендерим магазин, чтобы цены-замки обновились (страницу передаём
    # аргументом: cb.data — frozen, мутировать нельзя, v1.5.18). Возвращаем
    # на ТО ЖЕ страницу, где была покупка (не сбрасываем на первую).
    await shop_screen(cb, session, page=_SHOP_PAGE_CTX.get(cb.message.chat.id, 0))


@router.callback_query(F.data == "pet:inv")
@router.callback_query(F.data.startswith("inv:page:"))
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
            reply_markup=pet_hub(1),
        )
        await cb.answer()
        return

    try:
        page = int(cb.data.split(":")[2]) if cb.data.startswith("inv:page:") else 0
    except (IndexError, ValueError):
        page = 0
    # КЛЮЧЕВОЕ (v1.5.48, как в магазине): контентные кнопки строятся для ВСЕХ
    # предметов, страницы режет paged_pages, и они передаются в paged_keyboard
    # готовыми. Раньше total считался по плейсхолдерам, а кнопки резались
    # заново только из chunk → при page=0 ◀️/▶️ не создавались (листание
    # инвентаря было невозможно).
    content_buttons = []
    for inv, item in rows:
        content_buttons.append(InlineKeyboardButton(
            text=f"🎯 Использовать {item.icon} {html.escape(item.name)} ×{inv.quantity}",
            callback_data=f"use:{item.id}"))
    all_pages = paged_pages(content_buttons)
    total_pages = len(all_pages)
    page = max(0, min(page, total_pages - 1))
    chunk = rows[page * 6:(page + 1) * 6]
    lines = ["🎒 <b>Инвентарь</b>"
             + (f" · стр. {page + 1}/{total_pages}" if total_pages > 1 else ""), ""]
    for inv, item in chunk:
        lines.append(f"{item.icon} {html.escape(item.name)} ×{inv.quantity}")
    kb, page = paged_keyboard(
        content_buttons, prefix="inv", title="🎒 Инвентарь", page=page,
        back_cb="menu:pet", home_cb="menu:main", pages=all_pages,
    )
    await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("use:"))
async def use_item(cb: CallbackQuery, session: AsyncSession) -> None:
    try:
        item_id = int(cb.data.split(":")[1])
    except (IndexError, ValueError):
        return await cb.answer("Битая кнопка 😅", show_alert=True)
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
    if item is None:
        return await cb.answer("Предмет не найден", show_alert=True)

    svc = TamagotchiService(session)
    buff_note = ""
    # v1.5.68: лекарства на прогулке не дают (как мыться/тренироваться) —
    # проверяем ДО heal(), чтобы таблетка не списывалась впустую.
    if item.type == "medicine" and svc.on_walk(pet):
        return await cb.answer(t("pet.walk_deny_medicine", name=pet.name),
                               show_alert=True)
    if item.type in ("food", "drink"):
        result = await svc.feed(pet, {k: v for k, v in item.effect.items()})
        # если накормили реально (не кулдаун/сон) — покажем активные бафы
        buffs = svc.active_buffs(pet)
        if buffs:
            labels = [b.get("label") or b["type"]
                      for b in (pet.settings_extra or {}).get("buffs", [])]
            buff_note = "\n🔥 Активные бафы: " + ", ".join(dict.fromkeys(labels))
    elif item.type == "medicine":
        if item.code == "med_pill":
            result = await svc.heal(pet)
        else:
            if pet.is_sleeping:
                result = t("pet.sleeping_deny_heal")
            elif svc.on_walk(pet):
                result = t("pet.walk_deny_medicine")
            else:
                for stat, delta in item.effect.items():
                    from app.utils.formatting import clamp
                    setattr(pet, stat, clamp(getattr(pet, stat) + delta))
                result = f"{item.icon} {item.name} применён!"
    elif item.type == "toy":
        result = await svc.play(pet, won=False)  # игрушка = пассивная игра без проигрыша
    else:
        result = "❓ Этот предмет пока нельзя использовать."

    # v1.5.68: если применение отклонено (спит/на прогулке/критическое состояние/
    # кулдаун/здоров), предмет НЕ списываем и действие не логируем — иначе еда
    # «исчезала» во сне, а по кнопке «Разбудить» в ответе кормление не срабатывало.
    denied_markers = ("😴", "🚨", "⏳", "😀 Питомец здоров", "запыхался",
                      "пока нельзя")
    if any(m in result for m in denied_markers):
        return await cb.answer(result, show_alert=True)

    inv.quantity -= 1
    if inv.quantity <= 0:
        await session.delete(inv)
    await session.flush()
    await pets.log_action(pet.id, "use", meta={"item": item.code})
    try:
        await safe_edit_or_answer(cb.message, f"{result}{buff_note}\n\n" + await svc.render_async(pet),
                                  reply_markup=pet_hub(1))
    finally:
        # коммит в finally: edit уже неотменить, а без commit'а при сетевом
        # сбое middleware откатит сессию — предмет исчез бы из UI, но остался
        # в инвентаре (рассинхрон).
        await session.commit()
    # визуальный эффект по типу предмета: 🎁/❤️‍🩹/🥳 вместо тихого ack
    fx_kind = {"food": "feed", "drink": "item", "medicine": "heal",
               "toy": "play"}.get(item.type, "item")
    from app.utils.fx import apply_effect
    await apply_effect(cb, fx_kind, toast_override=result.split("\n")[0][:200] or None)
