"""🧢 Мерч канала — отдельное меню, не связанное с питомцем.

Мерч — это про сам канал (футболки, худи, кружки, стикерпаки), поэтому он
вынесен из 🛒 Магазина питомца в собственный раздел главного меню:

    menu:merch          → категории (👕 Футболки / 🧥 Худи / ☕ Аксессуары)
    merch:cat:<code>    → товары категории
    merch:item:<id>     → карточка товара (размер/описание/цена)
    merch:buy:<id>      → оформление покупки
    merch:ok:<id>       → подтверждение заказа

Данные берутся из конфига (MERCH_ITEMS) либо из дефолтной витрины ниже.
Формат MERCH_ITEMS (через ; между позициями):
    Категория|Название|Цена ₽|Описание|Размеры(S,M,L,XL)
Первые два сегмента обязательны; остальные — по желанию.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

from app.config import get_settings
from app.db.repositories import UserRepository
from app.utils.safe_edit import safe_edit_or_answer

router = Router(name="merch")

# ---------------------------------------------------------------------------
# Витрина мерча
# ---------------------------------------------------------------------------

CATEGORIES: dict[str, tuple[str, str]] = {  # code -> (emoji, название)
    "tshirt": ("👕", "Футболки"),
    "hoodie": ("🧥", "Худи"),
    "acc":    ("☕", "Аксессуары"),
}

DEFAULT_MERCH: list[dict] = [
    dict(cat="tshirt", name="Футболка «Не флуди»", price=1490, icon="👕",
         description="Плотный хлопок 180 г/м², печатный принт маскота на спине.",
         sizes=["S", "M", "L", "XL"]),
    dict(cat="tshirt", name="Футболка «Ban is temporary»", price=1490, icon="👕",
         description="Чёрная базовая с цитатой из шапки канала.",
         sizes=["S", "M", "L", "XL", "XXL"]),
    dict(cat="hoodie", name="Худи канала", price=3290, icon="🧥",
         description="Флис 320 г/м², капюшон, внутренний карман. Тираж 50 шт.",
         sizes=["S", "M", "L", "XL"]),
    dict(cat="hoodie", name="Оверсайз-худи «Tamagotchi»", price=3690, icon="🧥",
         description="Свободный крой, вышивка пиксельного питомца на груди.",
         sizes=["M", "L", "XL"]),
    dict(cat="acc", name="Кружка «Модератор»", price=690, icon="☕",
         description="Керамика 330 мл, двусторонняя печать.", sizes=[]),
    dict(cat="acc", name="Стикерпак", price=350, icon="🎨",
         description="10 виниловых наклеек, не боятся воды.", sizes=[]),
]


def _parse_items() -> list[dict]:
    """Витрина из MERCH_ITEMS («Категория|Название|Цена|Описание|Размеры»), иначе дефолт."""
    raw = (get_settings().merch_items or "").strip()
    if not raw:
        return [dict(m, sizes=list(m["sizes"])) for m in DEFAULT_MERCH]
    out: list[dict] = []
    for i, chunk in enumerate(raw.split(";")):
        parts = [p.strip() for p in chunk.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        cat = parts[0].lower()
        if cat not in CATEGORIES:
            cat = "acc"
        try:
            price = int(float(parts[2].replace("₽", "").strip())) if len(parts) > 2 else 0
        except ValueError:
            price = 0
        sizes = ([s.strip() for s in parts[4].split(",") if s.strip()]
                 if len(parts) > 4 and parts[4] else [])
        out.append(dict(cat=cat, name=parts[1], price=price,
                        icon=CATEGORIES[cat][0],
                        description=parts[3] if len(parts) > 3 else "",
                        sizes=sizes))
    return out


def _items_by_cat() -> dict[str, list[tuple[int, dict]]]:
    groups: dict[str, list[tuple[int, dict]]] = {c: [] for c in CATEGORIES}
    items = _parse_items()
    for idx, it in enumerate(items):
        groups.setdefault(it["cat"], []).append((idx, it))
    return groups


def _find(idx_str: str) -> dict | None:
    try:
        idx = int(idx_str)
    except ValueError:
        return None
    items = _parse_items()
    return items[idx] if 0 <= idx < len(items) else None


# ---------------------------------------------------------------------------
# Навигация
# ---------------------------------------------------------------------------

def _back_kb(target: str, label: str = "⬅️ Назад") -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.button(text=label, callback_data=target)
    return b


@router.callback_query(F.data == "menu:merch")
async def merch_screen(cb: CallbackQuery, session=None) -> None:
    """Главный экран мерча: категории."""
    settings = get_settings()
    groups = _items_by_cat()
    lines = ["🧢 <b>Мерч канала</b>",
             "Одежда и атрибутика для своих. Выбирай категорию 👇", ""]
    b = InlineKeyboardBuilder()
    for code, (icon, title) in CATEGORIES.items():
        n = len(groups.get(code, []))
        if n:
            lines.append(f"{icon} <b>{title}</b> — {n} шт.")
            b.button(text=f"{icon} {title} ({n})", callback_data=f"merch:cat:{code}")
    b.adjust(1)
    if settings.merch_url:
        b.row()
        b.button(text="🌐 Открыть магазин мерча", url=settings.merch_url)
    b.button(text="⬅️ Назад", callback_data="menu:main")
    await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=b.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("merch:cat:"))
async def merch_category(cb: CallbackQuery) -> None:
    code = cb.data.split(":")[2] if len(cb.data.split(":")) > 2 else ""
    if code not in CATEGORIES:
        return await cb.answer("Категория не найдена 😅", show_alert=True)
    icon, title = CATEGORIES[code]
    products = _items_by_cat().get(code, [])
    if not products:
        await safe_edit_or_answer(
            cb.message, f"{icon} <b>{html.escape(title)}</b>\n\nПока пусто — скоро новинки!",
            reply_markup=_back_kb("menu:merch", f"⬅️ К категориям").as_markup())
        return await cb.answer()
    lines = [f"{icon} <b>{html.escape(title)}</b>\n"]
    b = InlineKeyboardBuilder()
    for idx, it in products:
        sizes = f" · {'/'.join(it['sizes'])}" if it["sizes"] else ""
        lines.append(f"• <b>{html.escape(it['name'])}</b> — {it['price']:,} ₽{sizes}")
        b.button(text=f"{it['name']} · {it['price']} ₽", callback_data=f"merch:item:{idx}")
    b.adjust(1)
    b.button(text="⬅️ К категориям", callback_data="menu:merch")
    b.button(text="🏠 Меню", callback_data="menu:main")
    await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=b.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("merch:item:"))
async def merch_item(cb: CallbackQuery) -> None:
    it = _find(cb.data.split(":")[1])
    if it is None:
        return await cb.answer("Товар не найден 😅", show_alert=True)
    icon, cat_title = CATEGORIES[it["cat"]]
    sizes = ("\n📏 Размеры: " + ", ".join(it["sizes"])) if it["sizes"] else ""
    text = (f"{icon} <b>{html.escape(it['name'])}</b>\n"
            f"🧺 {html.escape(cat_title)}\n"
            f"💳 {it['price']:,} ₽\n"
            f"📝 {html.escape(it['description'])}{sizes}\n\n"
            "Оформим заказ — менеджер напишет тебе в ЛС после подтверждения 🤝")
    b = InlineKeyboardBuilder()
    b.button(text="✅ Заказать", callback_data=f"merch:buy:{cb.data.split(':')[1]}")
    b.row()
    b.button(text="⬅️ К товарам", callback_data=f"merch:cat:{it['cat']}")
    b.button(text="🧢 Категории", callback_data="menu:merch")
    await safe_edit_or_answer(cb.message, text, reply_markup=b.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("merch:buy:"))
async def merch_buy(cb: CallbackQuery, session) -> None:
    """Подтверждение заказа: пишем в logs/merch_orders.jsonl и просим контакт."""
    it = _find(cb.data.split(":")[1])
    if it is None:
        return await cb.answer("Товар не найден 😅", show_alert=True)
    users = UserRepository(session)
    user = await users.get(cb.from_user.id)
    order_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    record = {
        "order_id": order_id,
        "tg_id": cb.from_user.id,
        "username": cb.from_user.username,
        "display_name": (user.first_name or user.username) if user else cb.from_user.full_name,
        "item": it["name"], "category": CATEGORIES[it["cat"]][1],
        "price_rub": it["price"], "created_at": order_id,
    }
    try:
        from pathlib import Path
        path = Path("logs/merch_orders.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info("merch order #{} by {} — {}", order_id, cb.from_user.id, it["name"])
    except OSError:
        logger.warning("не удалось записать заказ мерча в файл")

    settings = get_settings()
    b = InlineKeyboardBuilder()
    if settings.merch_url:
        b.button(text="💬 Написать менеджеру", url=settings.merch_url)
        b.row()
    b.button(text="⬅️ Назад к товару", callback_data=f"merch:item:{cb.data.split(':')[1]}")
    await cb.message.answer(
        f"✅ Заказ <b>№{order_id}</b> принят!\n"
        f"{it['icon']} {html.escape(it['name'])} — {it['price']:,} ₽\n\n"
        "Мы сохранили заявку и свяжемся с тобой для уточнения размера и доставки 🚚",
        parse_mode="HTML",
    )
    await cb.answer("Заявка отправлена ✅")


@router.callback_query(F.data == "merch:noop")
async def merch_noop(cb: CallbackQuery) -> None:
    await cb.answer()
