from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Item, PetInventory, User
from app.db.repositories import PetRepository, UserRepository
from app.handlers.tamagotchi import set_pet_page
from app.i18n import t
from app.keyboards.inline import InlineKeyboardBuilder, InlineKeyboardButton, pet_hub
from app.keyboards.paged import paged_keyboard, paged_pages
from app.services.tamagotchi import TamagotchiService
from app.utils.safe_edit import safe_edit_or_answer

router = Router(name="shop")


def _fmt_dur(sec: int) -> str:
    """3600 -> '1 ч', 1800 -> '30 мин' — тот же формат, что в карточке питомца."""
    return TamagotchiService._fmt_dur(sec)


def _pct(v: float) -> str:
    """0.25 -> '+25%', -0.5 -> '-50%' (без хвостовых нулей: 0.15 -> '+15%')."""
    p = v * 100
    s = f"{p:.10g}"
    return f"+{s}%" if p > 0 else f"{s}%"


def _effect_phrase(effect: dict) -> str:
    """Мгновенные статы предмета — из того же словаря effect, что применяет feed().

    Порядок и подписи соответствуют STAT_EMOJI в справочнике; buff-ключ
    пропускается (его описывает _buff_phrase).
    """
    from app.services.pet_manual import STAT_EMOJI

    order = ["hunger", "happiness", "energy", "hygiene", "health"]
    bits = [f"{STAT_EMOJI[k]}{'+' if effect[k] >= 0 else ''}{effect[k]:g}"
            for k in order
            if isinstance(effect.get(k), (int, float)) and effect[k]]
    stat_names = {"strength": "💪+1", "agility": "🏃+1", "intellect": "🧠+1"}
    bits += [stat_names[k] for k in ("strength", "agility", "intellect")
             if isinstance(effect.get(k), (int, float)) and effect[k]]
    return " · ".join(bits)


def _buff_phrase(effect: dict) -> str:
    """Описание бафов предмета — генерируется из effect['buff']/effect['buffs'].

    Раньше здесь были литералы («⚡+40; час сон восстанавливает ×2 ⚡…»),
    которые молча расходились с механикой при правке чисел в effect.
    Теперь цифры берутся прямо из тех же словарей, которые читает feed().
    """
    defs = list(effect.get("buffs") or [])
    if isinstance(effect.get("buff"), dict):
        defs = [effect["buff"], *defs]

    def _kind_txt(tname: str, m: float) -> str:
        if tname == "no_decay":
            # no_decay вычитается из множителя спада: mult=0.5 → «вдвое медленнее»
            return ("энергия почти не тратится" if m >= 1
                    else f"энергия тратится медленнее на {abs(m) * 100:.10g}%")
        plain = {
            "food_feast": "еда усваивается",
            "happy_pct": "игры дают счастье",
            "energy_regen_pct": "сон восстанавливает энергию",
            "train_pct": "тренировки",
            "xp_pct": "все дела дают XP",
            "coin_pct": "монеты с прогулок",
        }
        label = plain.get(tname)
        if label is None:
            return f"баф {tname}"
        return f"{label} {_pct(m)}"

    parts = []
    for b in defs:
        if not isinstance(b, dict) or not b.get("type"):
            continue
        tname, mult = str(b["type"]), float(b.get("mult", 0.0))
        dur = _fmt_dur(int(b.get("duration", 1800)))
        parts.append(f"{dur} — {_kind_txt(tname, mult)}")
    return "; ".join(parts)


def _item_desc(effect: dict, flavor: str) -> str:
    """Полное описание товара: живые цифры эффекта + авторская подводка."""
    eff_bits = [x for x in (_effect_phrase(effect), _buff_phrase(effect)) if x]
    eff = " · ".join(eff_bits)
    if not flavor:
        return eff
    return f"{eff}. {flavor}" if eff else flavor

# LRU-контейнер из раздела питомца: обычный dict растёт без ограничений —
# каждый когда-либо открывавший магазин чат оставлял бы запись навсегда
# (утечка памяти на долгих аптаймах).
from app.handlers.tamagotchi import _BoundedChatCtx

_SHOP_PAGE_CTX = _BoundedChatCtx()



def _nav_back_cb(cb: CallbackQuery, section: str) -> str | None:
    """Callback кнопки «⬅️ Назад» для постраничных экранов магазина/инвентаря.

    Единый источник истины — `inline._nav_back_cb` (тот же алгоритм, что у
    всех остальных экранов): учитывает историю переходов, корень раздела и
    защиту от самопетли. Локальная копия логики раньше расходилась с общей
    и возвращала «Назад в себя» (кнопка «ничего не делает»).

    current_cb = нажатая кнопка входа ('pet:shop'/'pet:inv'): включает
    режим current_source — подраздел хаба открывается ОТДЕЛЬНЫМ сообщением
    поверх вкладки 'pet:page:N', поэтому «Назад» должен вести на вкладку,
    а не подавляться как «самопетля к корню раздела»."""
    from app.keyboards.inline import _nav_back_cb as _common
    chat_id = cb.message.chat.id if cb.message else None
    entry = {"shop": "pet:shop", "inv": "pet:inv"}.get(section)
    return _common(section, chat_id, current_cb=entry)


# Описания товаров генерируются из effect (единый источник правды: его же
# применяет feed()/use-логика магазина) плюс короткая авторская подводка без
# цифр (ключ flavor). Раньше числа дублировались литералами в description и
# молча расходились с механикой; теперь description физически не может
# содержать устаревшую цифру — он пересобирается из effect на импорте.
ITEMS_SEED = [
    dict(code="food_bread", name="Хлеб", icon="🍞", type="food", price=5,
         effect={"hunger": 15}, flavor="Просто, дёшево, сердито."),
    dict(code="food_apple", name="Яблоко", icon="🍎", type="food", price=10,
         effect={"hunger": 25, "health": 3}, flavor="Полезно!"),
    dict(code="food_carrot", name="Морковка", icon="🥕", type="food", price=8,
         effect={"hunger": 18, "hygiene": 3}, flavor="Сытно и зубам хорошо."),
    dict(code="food_fish", name="Рыбка", icon="🐟", type="food", price=18,
         effect={"hunger": 32, "strength": 1}, flavor="Камчатский улов."),
    dict(code="food_meat", name="Стейк", icon="🥩", type="food", price=25,
         effect={"hunger": 45, "strength": 1}, flavor="Сытно и для силы."),
    dict(code="food_soup", name="Горячий суп", icon="🍲", type="food", price=20,
         effect={"hunger": 38, "health": 5}, flavor="Согревает и лечит чуть-чуть."),
    dict(code="food_cake", name="Тортик", icon="🍰", type="food", price=40,
         effect={"hunger": 30, "happiness": 15, "hygiene": -5},
         flavor="Вкусно, но потом мыться!"),
    dict(code="food_donut", name="Пончик", icon="🍩", type="food", price=15,
         effect={"hunger": 18, "happiness": 8}, flavor="Сахарное счастье."),
    dict(code="food_sushi", name="Сендвич сёмги", icon="🍣", type="food", price=45,
         effect={"hunger": 42, "happiness": 8, "intellect": 1},
         flavor="Дальневосточный деликатес."),
    dict(code="food_feast", name="Праздничный ужин", icon="🦞", type="food", price=70,
         effect={"hunger": 60, "happiness": 10,
                 "buff": {"type": "food_feast", "mult": 0.25, "duration": 1800,
                          "label": "Сытный час"}}),
    dict(code="food_honey", name="Бочонок мёда", icon="🍯", type="food", price=55,
         effect={"hunger": 30, "health": 8,
                 "buff": {"type": "happy_pct", "mult": 0.20, "duration": 3600,
                          "label": "Медовое настроение"}}),
    dict(code="drink_water", name="Водичка", icon="💧", type="drink", price=4,
         effect={"energy": 5, "hygiene": -2}, flavor="Просто попить."),
    dict(code="drink_juice", name="Сок", icon="🧃", type="drink", price=10,
         effect={"energy": 10, "happiness": 3}, flavor="Витаминный заряд."),
    dict(code="drink_milk", name="Молоко", icon="🥛", type="drink", price=12,
         effect={"energy": 12, "health": 3, "hunger": 5}, flavor="Крепкие кости."),
    dict(code="drink_coffee", name="Кофе", icon="☕", type="drink", price=20,
         effect={"energy": 20,
                 "buff": {"type": "no_decay", "mult": 0.5, "duration": 1800,
                          "label": "Кофеиновый щит"}}),
    dict(code="drink_energy", name="Энергетик", icon="⚡", type="drink", price=35,
         effect={"energy": 40, "happiness": -3,
                 "buffs": [
                     {"type": "energy_regen_pct", "mult": 1.0, "duration": 3600,
                      "label": "Передоз бодрости"},
                     {"type": "train_pct", "mult": 0.5, "duration": 3600,
                      "label": "Предтрен"},
                 ]},
         flavor="Бодрость любой ценой."),
    dict(code="drink_tea", name="Иван-чай", icon="🍵", type="drink", price=15,
         effect={"energy": 8, "health": 4}, flavor="Камчатский травяной, бодрит мягко."),
    dict(code="drink_smoothie", name="Смузи из ягод", icon="🫐", type="drink", price=25,
         effect={"energy": 15, "happiness": 6,
                 "buff": {"type": "xp_pct", "mult": 0.15, "duration": 3600,
                          "label": "Ягодная ясность"}}),
    dict(code="toy_ball", name="Мячик", icon="⚽", type="toy", price=30,
         effect={"happiness": 10}, flavor="Игрушка: играет сам."),
    dict(code="toy_laser", name="Лазерная указка", icon="🔦", type="toy", price=80,
         effect={"happiness": 20, "agility": 1}, flavor="Кошачий экстаз."),
    dict(code="toy_puzzle", name="Головоломка", icon="🧩", type="toy", price=60,
         effect={"happiness": 12, "intellect": 1}, flavor="Ум растёт, лапы не устают."),
    dict(code="med_pill", name="Лекарство", icon="💊", type="medicine", price=35,
         effect={"health": 35}, flavor="Лечит болезни."),
    dict(code="med_vitamins", name="Витамины", icon="🧪", type="medicine", price=60,
         effect={"health": 15, "energy": 15}, flavor="Бодрость и здоровье."),
    dict(code="med_syrup", name="Сироп от кашля", icon="🍯", type="medicine", price=45,
         effect={"health": 25}, flavor="Мягкое лечение, быстрее ставит на лапы."),
]


# Нормализация: description каждого товара обязан быть собран из его же
# effect (см. _item_desc). Даже если кто-то добавит новый товар и укажет
# description вручную — на импорте оно будет перезаписано живыми цифрами.
for _spec in ITEMS_SEED:
    _spec["description"] = _item_desc(_spec["effect"], _spec.pop("flavor", ""))


def _seed_kwargs(spec: dict) -> dict:
    """Только колонки Item: защита от опечатки в ключе seed (TypeError при
    конструировании модели ломал бы весь прогон тестов с БД)."""
    cols = {c.name for c in Item.__table__.columns}
    unknown = set(spec) - cols
    if unknown:
        raise KeyError(f"ITEMS_SEED: неизвестные ключи {unknown} у {spec.get('code')}")
    return spec


async def seed_items(session: AsyncSession) -> int:
    existing = set((await session.execute(select(Item.code))).scalars())
    created = 0
    next_id = ((await session.execute(select(Item.id).order_by(Item.id.desc()).limit(1)))
               .scalars().first() or 0)
    for spec in ITEMS_SEED:
        if spec["code"] in existing:
            continue
        next_id += 1
        session.add(Item(id=next_id, **_seed_kwargs(spec)))
        created += 1
    if created:
        await session.flush()
    return created

def shop_keyboard(items: list[Item], user_coins: int) -> InlineKeyboardBuilder | None:
    b = InlineKeyboardBuilder()
    for it in items:
        if it.type == "merch":
            continue
        afford = "🪙" if user_coins >= it.price else "🔒"
        b.button(text=f"{afford} {it.icon} {html.escape(it.name)} · {it.price}",
                 callback_data=f"buy:{it.id}")
    b.adjust(1)
    return b

@router.callback_query(F.data == "pet:shop")
@router.callback_query(F.data.startswith("shop:page:"))
@router.callback_query(F.data.startswith("shop:back"))
@router.callback_query(F.data.startswith("shop:next"))
async def shop_screen(cb: CallbackQuery, session: AsyncSession,
                      page: int | None = None) -> None:
    set_pet_page(cb.message.chat.id, 1)
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
    if page is None:
        data = cb.data or ""
        try:
            if data.startswith("shop:page:"):
                page = int(data.split(":")[2])
            elif data.startswith(("shop:back", "shop:next")):
                cur = _SHOP_PAGE_CTX.get(cb.message.chat.id, 0)
                page = cur - 1 if data.startswith("shop:back") else cur + 1
            else:
                page = 0
        except (IndexError, ValueError):
            page = 0
    else:
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = 0
    grouped = [it for t in ("food", "drink", "toy", "medicine")
               for it in items if it.type == t] + \
              [it for it in items if it.type not in ("food", "drink", "toy", "medicine")]
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

    kb, page = paged_keyboard(
        content_buttons, prefix="shop", title="🛒 Магазин", page=page,
        back_cb=_nav_back_cb(cb, "shop"), pages=all_pages,
        home_cb="menu:main",
    )
    _SHOP_PAGE_CTX.set(cb.message.chat.id, page)
    await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data.in_({"shop:noop", "inv:noop"}))
async def shop_noop(cb: CallbackQuery) -> None:
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
        from app.handlers.merch import merch_screen
        await cb.answer()
        return await merch_screen(cb)
    if user.coins < item.price:
        await cb.answer(f"Не хватает {item.price - user.coins} монет 🪙", show_alert=True)
        return

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
    await session.commit()
    logger.info(f"user {user.tg_id} bought {item.code} for {item.price}")
    await cb.answer(f"🛒 Куплено: {item.icon} {item.name}!", show_alert=False)
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
    # Кнопка предмета — короткая подпись «🍞 Хлеб x1»: на мобильных экранах
    # длинный «Использовать …» обрезался и было непонятно, что внутри; теперь
    # количество всегда видно. Само использование — через всплывающее окно
    # подтверждения («use_ok:…»), см. use_confirm_screen.
    content_buttons = []
    for inv, item in rows:
        content_buttons.append(InlineKeyboardButton(
            text=f"{item.icon} {html.escape(item.name)} x{inv.quantity}",
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
        back_cb=_nav_back_cb(cb, "inv"), pages=all_pages,
        home_cb="menu:main",
    )
    await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data.startswith("use:"))
async def use_confirm_screen(cb: CallbackQuery, session: AsyncSession) -> None:
    """Всплывающее окно подтверждения использования предмета из инвентаря.

    Тап по «🍞 Хлеб x1» ничего не тратит: показывается экран-подтверждение
    с описанием предмета и его эффекта, кнопками «✅ Использовать» (use_ok)
    и «⬅️ Назад» (возврат в инвентарь). Реальное применение — в use_item.
    """
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

    effect_labels = {"hunger": "🍖 Сытость", "health": "❤️ Здоровье",
                     "hygiene": "🫧 Гигиена", "happiness": "😀 Счастье",
                     "strength": "💪 Сила", "intellect": "🧠 Интеллект",
                     "energy": "⚡ Энергия"}
    if item.effect:
        eff = ", ".join(
            f"{effect_labels.get(k, k)} {'+' if v >= 0 else ''}{v}"
            for k, v in item.effect.items())
    else:
        eff = "—"
    text = (
        f"🎒 <b>Использовать предмет?</b>\n\n"
        f"{item.icon} <b>{html.escape(item.name)}</b> ×{inv.quantity}\n"
        f"Эффект: {eff}\n"
    )
    if item.description:
        text += f"«{html.escape(item.description)}»\n"
    text += (f"\nПрименить к питомцу <b>{html.escape(pet.name)}</b>? "
             f"Один предмет будет потрачен.")

    b = InlineKeyboardBuilder()
    b.button(text="✅ Использовать", callback_data=f"use_ok:{item_id}")
    back = _nav_back_cb(cb, "inv") or "pet:inv"
    b.button(text="⬅️ Назад", callback_data=back)
    await safe_edit_or_answer(cb.message, text, reply_markup=b.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("use_ok:"))
async def use_item(cb: CallbackQuery, session: AsyncSession) -> None:
    """Реальное применение предмета — вызывается только после подтверждения."""
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
    # Страж состояний: спящему/гуляющему нельзя использовать вещи из
    # инвентаря (еда, лекарства, игрушки). Единый отказ + точечная подсказка.
    use_action = {"food": "feed", "drink": "feed",
                  "medicine": "heal", "toy": "play"}.get(item.type or "")
    if use_action:
        deny = svc.state_deny(pet, use_action)
        if deny:
            hint = svc.sleeping_hint(use_action) if pet.is_sleeping else None
            return await cb.answer(hint or deny, show_alert=True)
    if item.type == "medicine" and svc.on_walk(pet):
        return await cb.answer(t("pet.walk_deny_medicine", name=pet.name),
                               show_alert=True)
    if item.type in ("food", "drink"):
        # with_result=True: успех/отказ берётся из флага, а не из поиска
        # эмодзи в тексте (старые denied_markers ломались на любой теме,
        # где перевод строки отказа отличался от стандартного).
        result, used = await svc.feed(pet, dict(item.effect.items()),
                                      with_result=True)
        if not used:
            return await cb.answer(result, show_alert=True)
        buffs = svc.active_buffs(pet)
        if buffs:
            labels = [b.get("label") or b["type"]
                      for b in (pet.settings_extra or {}).get("buffs", [])]
            buff_note = "\n🔥 Активные бафы: " + ", ".join(dict.fromkeys(labels))
    elif item.type == "medicine":
        if item.code == "med_pill":
            result, used = await svc.heal(pet, with_result=True)
        else:
            if pet.is_sleeping:
                result, used = t("pet.sleeping_deny_heal"), False
            elif svc.on_walk(pet):
                result, used = t("pet.walk_deny_medicine"), False
            else:
                for stat, delta in item.effect.items():
                    from app.utils.formatting import clamp
                    setattr(pet, stat, clamp(getattr(pet, stat) + delta))
                result, used = f"{item.icon} {item.name} применён!", True
        if not used:
            return await cb.answer(result, show_alert=True)
    elif item.type == "toy":
        result, used = await svc.play(pet, won=False, with_result=True)
        if not used:
            return await cb.answer(result, show_alert=True)
    else:
        return await cb.answer(t("pet.item_not_usable"), show_alert=True)

    inv.quantity -= 1
    if inv.quantity <= 0:
        await session.delete(inv)
    await session.flush()
    await pets.log_action(pet.id, "use", meta={"item": item.code})
    try:
        await safe_edit_or_answer(cb.message, f"{result}{buff_note}\n\n" + await svc.render_async(pet),
                                  reply_markup=pet_hub(1))
    finally:
        await session.commit()
    fx_kind = {"food": "feed", "drink": "item", "medicine": "heal",
               "toy": "play"}.get(item.type, "item")
    from app.utils.fx import apply_effect
    await apply_effect(cb, fx_kind, toast_override=result.split("\n")[0][:200] or None)
