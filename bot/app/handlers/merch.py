from __future__ import annotations

import html
import secrets

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, InlineKeyboardButton, InputMediaPhoto, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

from app.config import get_settings
from app.db.repositories import MerchRepository
from app.utils.safe_edit import safe_edit_or_answer

router = Router(name="merch")

def _is_merch_admin(user_id: int) -> bool:
    s = get_settings()
    if user_id in (s.admin_ids or []):
        return True
    return bool(s.merch_admin_id) and user_id == s.merch_admin_id

def _variant_caption(product, v) -> str:
    lines = [f"🧢 <b>{html.escape(product.name)}</b>",
             f"📏 Размер: <b>{html.escape(v.size or '—')}</b> · 🎨 Цвет: <b>{html.escape(v.color or '—')}</b>",
             f"💳 Цена: <b>{v.price_rub:,} ₽</b>"]
    if v.stock <= 0:
        lines.append("📦 Остаток: <b>нет в наличии</b>")
    elif v.reserved_by is not None:
        lines.append(f"📦 Остаток: {v.stock} шт. (⏳ забронировано)")
    else:
        lines.append(f"📦 Остаток: <b>{v.stock} шт.</b>")
    if product.description:
        lines.append(f"📝 {html.escape(product.description)}")
    return "\n".join(lines)

async def _notify_merch_admins(bot: Bot, text: str, kb) -> None:
    s = get_settings()
    targets = set(s.admin_ids or [])
    if s.merch_admin_id:
        targets.add(s.merch_admin_id)
    for uid in targets:
        try:
            await bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
        except Exception as exc:
            logger.debug("merch notify {} failed: {}", uid, type(exc).__name__)

@router.callback_query(F.data == "menu:merch")
async def merch_screen(cb: CallbackQuery, session) -> None:
    repo = MerchRepository(session)
    cats = await repo.categories()
    lines = ["🧢 <b>Мерч канала</b>",
             "",
             "Одежда и атрибутика с фирменными принтами. Выбирай категорию 👇"]
    b = InlineKeyboardBuilder()
    for c in cats:
        products = await repo.products(c.id)
        b.button(text=f"{c.icon} {c.title} · {len(products)} моделей",
                 callback_data=f"merch:cat:{c.code}")
        b.row()
    if not cats:
        lines.append("\nКаталог пока пуст — скоро новинки!")
    s = get_settings()
    if s.merch_url:
        b.button(text="🌐 Открыть магазин мерча", url=s.merch_url)
        b.row()
    b.button(text="🏠 Меню", callback_data="menu:main")
    await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("merch:cat:"))
async def merch_category(cb: CallbackQuery, session) -> None:
    code = cb.data.split(":")[2]
    repo = MerchRepository(session)
    cat = await repo.get_category(code)
    if cat is None:
        return await cb.answer("Категория не найдена 😅", show_alert=True)
    products = await repo.products(cat.id)
    lines = [f"{cat.icon} <b>{html.escape(cat.title)}</b>", ""]
    b = InlineKeyboardBuilder()
    if not products:
        lines.append("Пока пусто — скоро новинки!")
    for p in products:
        variants = await repo.variants(p.id)
        total_stock = sum(v.stock for v in variants)
        price_min = min((v.price_rub for v in variants), default=0)
        lines.append(f"• <b>{html.escape(p.name)}</b> — от {price_min:,} ₽ · всего {total_stock} шт.")
        b.button(text=f"{cat.icon} {p.name}", callback_data=f"merch:prod:{p.id}")
        b.row()
    b.button(text="⬅️ К категориям", callback_data="menu:merch")
    b.button(text="🏠 Меню", callback_data="menu:main")
    await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("merch:prod:"))
async def merch_product(cb: CallbackQuery, session) -> None:
    try:
        pid = int(cb.data.split(":")[2])
    except ValueError:
        return await cb.answer()
    repo = MerchRepository(session)
    product = await repo.get_product(pid)
    if product is None:
        return await cb.answer("Товар не найден 😅", show_alert=True)
    from app.db.models import MerchCategory
    from sqlalchemy import select as _select
    cat = (await session.execute(
        _select(MerchCategory).where(MerchCategory.id == product.category_id)
    )).scalar_one_or_none()
    icon = cat.icon if cat else "🧢"
    back_cb = f"merch:cat:{cat.code}" if cat else "menu:merch"
    sizes = [x for x in (product.sizes or []) if x and x != "one"]
    colors = [c for c in (product.colors or []) if c]
    variants = await repo.variants(pid)
    b = InlineKeyboardBuilder()
    if sizes:
        lines = [f"{icon} <b>{html.escape(product.name)}</b>", "", "📏 Выбери размер:"]
        for sz in sizes:
            has = any(v.size == sz and v.stock > 0 for v in variants)
            b.button(text=f"{sz}{'' if has else ' ✖'}", callback_data=f"merch:size:{pid}:{sz}")
        b.row()
        b.button(text="⬅️ Назад", callback_data=back_cb)
        b.button(text="🏠 Меню", callback_data="menu:main")
        await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=b.as_markup())
        return await cb.answer()
    if colors:
        return await _render_variant_screen(cb, repo, product, "", colors[0], back_cb)
    if variants:
        return await _render_variant_screen(cb, repo, product, variants[0].size,
                                            variants[0].color, back_cb)
    await safe_edit_or_answer(
        cb.message,
        f"{icon} <b>{html.escape(product.name)}</b>\n\nДля этой модели ещё не заданы позиции.",
        reply_markup=None)
    await cb.answer()

@router.callback_query(F.data.startswith("merch:size:"))
async def merch_size(cb: CallbackQuery, session) -> None:
    parts = cb.data.split(":")
    try:
        pid = int(parts[2])
    except ValueError:
        return await cb.answer()
    size = parts[3] if len(parts) > 3 else ""
    repo = MerchRepository(session)
    product = await repo.get_product(pid)
    if product is None:
        return await cb.answer("Товар не найден 😅", show_alert=True)
    colors = [c for c in (product.colors or []) if c]
    variants = await repo.variants(pid)
    if not colors:
        match = next((v for v in variants if v.size == size), None)
        if match is None:
            return await cb.answer("Позиция не найдена 😅", show_alert=True)
        return await _render_variant_screen(cb, repo, product, size, match.color,
                                            f"merch:prod:{pid}")
    lines = [f"🎨 <b>{html.escape(product.name)}</b> · размер <b>{html.escape(size)}</b>", "",
             "Выбери цвет:"]
    b = InlineKeyboardBuilder()
    for c in colors:
        v = next((x for x in variants if x.size == size and x.color == c), None)
        ok = v is not None and v.stock > 0
        b.button(text=f"{c}{'' if ok else ' ✖'}",
                 callback_data=f"merch:var:{v.id if v else 0}")
    b.row()
    b.button(text="⬅️ К размерам", callback_data=f"merch:prod:{pid}")
    b.button(text="🏠 Меню", callback_data="menu:main")
    await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=b.as_markup())
    await cb.answer()

async def _render_variant_screen(cb: CallbackQuery, repo: MerchRepository,
                                 product, size: str, color: str, back_cb: str) -> None:
    v = await repo.find_variant(product.id, size, color)
    if v is None:
        await safe_edit_or_answer(
            cb.message,
            f"🧢 <b>{html.escape(product.name)}</b>\n\n"
            f"Позиции «{html.escape(size or '—')} / {html.escape(color or '—')}» нет в каталоге.",
            reply_markup=None)
        return await cb.answer()
    text = _variant_caption(product, v)
    photo = product.image_url
    b = InlineKeyboardBuilder()
    if v.stock > 0 and v.reserved_by is None:
        b.button(text="🛒 Забронировать", callback_data=f"merch:res:{v.id}")
        b.row()
    elif v.reserved_by is not None:
        text += "\n\n⏳ Эта позиция уже забронирована. Освободится после продажи или отмены брони."
    b.button(text="⬅️ Назад", callback_data=back_cb)
    b.button(text="🏠 Меню", callback_data="menu:main")
    msg = cb.message
    try:
        if photo:
            await msg.edit_media(media=InputMediaPhoto(media=photo, caption=text),
                                 reply_markup=b.as_markup())
        else:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=b.as_markup())
    except Exception:
        if photo:
            await msg.answer_photo(photo, caption=text, parse_mode="HTML",
                                   reply_markup=b.as_markup())
        else:
            await safe_edit_or_answer(msg, text, reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("merch:var:"))
async def merch_variant(cb: CallbackQuery, session) -> None:
    try:
        vid = int(cb.data.split(":")[2])
    except ValueError:
        return await cb.answer()
    repo = MerchRepository(session)
    v = await repo.get_variant(vid)
    if v is None:
        return await cb.answer("Позиция не найдена 😅", show_alert=True)
    product = await repo.get_product(v.product_id)
    if product is None:
        return await cb.answer()
    await _render_variant_screen(cb, repo, product, v.size, v.color,
                                 f"merch:size:{product.id}:{v.size}")

@router.callback_query(F.data.startswith("merch:res:"))
async def merch_reserve(cb: CallbackQuery, session, bot: Bot) -> None:
    try:
        vid = int(cb.data.split(":")[2])
    except ValueError:
        return await cb.answer()
    repo = MerchRepository(session)
    v = await repo.get_variant(vid)
    if v is None:
        return await cb.answer("Позиция не найдена 😅", show_alert=True)
    product = await repo.get_product(v.product_id)
    status = await repo.reserve(vid, cb.from_user.id)
    if status == "out_of_stock":
        return await cb.answer("Увы, нет в наличии 😔", show_alert=True)
    if status == "already_reserved":
        return await cb.answer("Эта позиция уже забронирована другим покупателем", show_alert=True)
    if status == "already_reserved_self":
        return await cb.answer("Ты уже забронировал эту позицию — жди сообщения менеджера 😉",
                               show_alert=True)
    await session.commit()
    name = cb.from_user.full_name
    uname = f"@{cb.from_user.username}" if cb.from_user.username else "без username"
    desc = f"{product.name} · {v.size or '—'} · {v.color or '—'} · {v.price_rub:,} ₽"
    admin_text = (f"🛒 <b>НОВАЯ БРОНЬ МЕРЧА</b>\n\n"
                  f"👤 {html.escape(name)} ({uname}, <code>{cb.from_user.id}</code>)\n"
                  f"🧢 {html.escape(desc)}\n\n"
                  f"Свяжись с покупателем для оплаты/доставки, затем подтверди продажу "
                  f"или отмени резерв.")
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить продажу", callback_data=f"merch:sold:{vid}")
    kb.button(text="❌ Отменить резерв", callback_data=f"merch:cancel:{vid}")
    kb.row()
    kb.button(text="📋 Все брони", callback_data="merch:myres")
    await _notify_merch_admins(bot, admin_text, kb.as_markup())
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К позиции", callback_data=f"merch:var:{vid}")
    b.button(text="🏠 Меню", callback_data="menu:main")
    await cb.message.answer(
        f"✅ <b>Бронь оформлена!</b>\n\n🧢 {html.escape(desc)}\n\n"
        "Менеджер канала свяжется с тобой в личных сообщениях для оплаты и доставки 🚚",
        parse_mode="HTML", reply_markup=b.as_markup())
    logger.info("merch reserved: variant={} by {}", vid, cb.from_user.id)
    await cb.answer("Забронировано ✅")

@router.callback_query(F.data.startswith("merch:sold:") | F.data.startswith("merch:cancel:"))
async def merch_admin_action(cb: CallbackQuery, session) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Это действие только для админов мерча 🙅", show_alert=True)
    parts = cb.data.split(":")
    action, sid = parts[1], parts[2]
    try:
        vid = int(sid)
    except ValueError:
        return await cb.answer()
    repo = MerchRepository(session)
    v = await repo.get_variant(vid)
    if v is None:
        return await cb.answer("Позиция не найдена 😅", show_alert=True)
    product = await repo.get_product(v.product_id)
    pname = product.name if product else f"#{vid}"
    if action == "sold":
        res = await repo.confirm_sale(vid)
        if res is None:
            return await cb.answer("Брони уже нет или нулевой остаток 😅", show_alert=True)
        await session.commit()
        buyer = res["buyer"]
        try:
            await cb.bot.send_message(
                buyer,
                f"🎉 <b>Поздравляем с покупкой!</b>\n\n🧢 {html.escape(pname)} · "
                f"{html.escape(v.size or '—')} · {html.escape(v.color or '—')} — {v.price_rub:,} ₽\n\n"
                "Спасибо, что ты с нами! Мерч уже едет к тебе 🚀",
                parse_mode="HTML")
        except Exception:
            pass
        await safe_edit_or_answer(
            cb.message,
            f"✅ Продажа подтверждена!\n\n🧢 {html.escape(pname)} · {v.size}/{v.color}\n"
            f"👤 Покупатель: <code>{buyer}</code>\n📦 Остаток обновлён.",
            reply_markup=None)
        logger.info("merch sale confirmed: variant={} buyer={}", vid, buyer)
    else:
        res = await repo.cancel_reserve(vid)
        if res is None:
            return await cb.answer("Брони уже нет 😅", show_alert=True)
        await session.commit()
        buyer = res["buyer"]
        try:
            await cb.bot.send_message(
                buyer,
                f"❌ Резерв отменён по позиции «{html.escape(pname)}».\n"
                "Если хочешь — забронируй заново в разделе «🧢 Наш мерч» 🧢",
                parse_mode="HTML")
        except Exception:
            pass
        await safe_edit_or_answer(
            cb.message,
            f"❌ Резерв отменён.\n\n🧢 {html.escape(pname)} · {v.size}/{v.color}\n"
            f"👤 Покупатель: <code>{buyer}</code>\nПозиция снова свободна.",
            reply_markup=None)
        logger.info("merch reserve cancelled: variant={} buyer={}", vid, buyer)
    await cb.answer("Готово ✅")

@router.callback_query(F.data == "merch:myres")
async def merch_my_reserves(cb: CallbackQuery, session) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    repo = MerchRepository(session)
    reserved = await repo.all_reserved()
    b = InlineKeyboardBuilder()
    if not reserved:
        text = "📋 Активных броней нет."
    else:
        lines = [f"📋 <b>Активные брони мерча ({len(reserved)})</b>", ""]
        for v in reserved[:25]:
            product = await repo.get_product(v.product_id)
            pname = product.name if product else f"#{v.product_id}"
            lines.append(f"• id={v.id} {html.escape(pname)} · {v.size}/{v.color} "
                         f"— 👤 <code>{v.reserved_by}</code>")
            b.button(text=f"✅ Продано #{v.id}", callback_data=f"merch:sold:{v.id}")
            b.button(text=f"❌ Снять #{v.id}", callback_data=f"merch:cancel:{v.id}")
            b.row()
        text = "\n".join(lines)
    b.button(text="⬅️ В мерч", callback_data="menu:merch")
    await safe_edit_or_answer(cb.message, text, reply_markup=b.as_markup())
    await cb.answer()

HELP_LINES = [
    "<b>Подкоманды:</b>",
    "/merch add_cat &lt;code&gt;|&lt;Название&gt;|&lt;эмодзи&gt; — новая категория",
    "/merch del_cat &lt;code&gt; — удалить категорию со всем содержимым",
    "/merch add_prod &lt;cat_code&gt;|&lt;Название&gt;|&lt;цена&gt;|&lt;остаток&gt; — товар со всеми размерами и цветами",
    "/merch add_var &lt;prod_id&gt;|&lt;размер&gt;|&lt;цвет&gt;|&lt;цена&gt;|&lt;остаток&gt; — одна позиция",
    "/merch stock &lt;variant_id&gt;|&lt;остаток&gt; — поправить остаток",
    "/merch del_var &lt;variant_id&gt; — удалить позицию",
    "/merch img &lt;prod_id&gt;|&lt;url&gt; — картинка товара",
    "/merch reserved — список активных броней",
]

DEFAULT_SIZES = ["S", "M", "L", "XL", "XXL"]
DEFAULT_COLORS = ["Розовый", "Чёрный", "Белый", "Серый"]

class MerchStates(StatesGroup):
    awaiting = State()

def _admin_kb(extra_rows=None) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    for row in (extra_rows or []):
        for text, cb_data in row:
            b.button(text=text, callback_data=cb_data)
        b.row()
    b.button(text="📦 Каталог", callback_data="madmin:catalog")
    b.button(text="📋 Брони", callback_data="merch:myres")
    b.row()
    b.button(text="➕ Новая категория", callback_data="madmin:addcat")
    b.row()
    b.button(text="⬅️ В магазин", callback_data="menu:merch")
    b.button(text="🏠 Меню", callback_data="menu:main")
    return b

async def _render_admin_home(cb: CallbackQuery, session) -> None:
    repo = MerchRepository(session)
    cats = await repo.categories()
    lines = ["🧢 <b>Управление мерчем</b>", ""]
    total_res = 0
    for c in cats:
        products = await repo.products(c.id)
        lines.append(f"{c.icon} <b>{html.escape(c.title)}</b> (<code>{c.code}</code>) — {len(products)} моделей")
        for p in products:
            variants = await repo.variants(p.id)
            stock = sum(v.stock for v in variants)
            res = sum(1 for v in variants if v.reserved_by is not None)
            total_res += res
            lines.append(f"   • id={p.id} {html.escape(p.name)} — позиций {len(variants)}, "
                         f"остаток {stock}, броней {res}")
    if total_res:
        lines += ["", f"⏳ Активных броней: <b>{total_res}</b>"]
    b = _admin_kb()
    await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=b.as_markup())

@router.callback_query(F.data == "madmin:home")
async def madmin_home(cb: CallbackQuery, session) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    await _render_admin_home(cb, session)
    await cb.answer()

@router.callback_query(F.data == "madmin:catalog")
async def madmin_catalog(cb: CallbackQuery, session) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    repo = MerchRepository(session)
    cats = await repo.categories()
    b = InlineKeyboardBuilder()
    for c in cats:
        products = await repo.products(c.id)
        b.button(text=f"{c.icon} {c.title} · {len(products)}", callback_data=f"madmin:cat:{c.code}")
        b.row()
    b.button(text="✏️ Изменить категории", callback_data="madmin:catmgmt")
    b.row()
    b.button(text="⬅️ Назад", callback_data="madmin:home")
    await safe_edit_or_answer(
        cb.message,
        "📦 <b>Каталог</b>\n\nВыбери категорию для управления товарами:",
        reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data == "madmin:catmgmt")
async def madmin_catmgmt(cb: CallbackQuery, session) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    repo = MerchRepository(session)
    cats = await repo.categories()
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить категорию", callback_data="madmin:addcat")
    b.row()
    for c in cats:
        b.button(text=f"✏️ {c.icon} {c.title}", callback_data=f"madmin:catedit:{c.code}")
        b.button(text="🗑", callback_data=f"madmin:catdel:{c.code}")
        b.row()
    b.button(text="⬅️ Назад", callback_data="madmin:catalog")
    b.button(text="🏠 Управление", callback_data="madmin:home")
    await safe_edit_or_answer(
        cb.message,
        "🗂 <b>Категории</b>\n\n✏️ — переименовать/сменить эмодзи,\n🗑 — удалить категорию со всем содержимым.",
        reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("madmin:catedit:"))
async def madmin_cat_edit(cb: CallbackQuery, session, state: FSMContext) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    code = cb.data.split(":")[2]
    repo = MerchRepository(session)
    cat = await repo.get_category(code)
    if cat is None:
        return await cb.answer("Категория не найдена 😅", show_alert=True)
    await state.set_state(MerchStates.awaiting)
    await state.update_data(step="cat_title", code=code)
    b = InlineKeyboardBuilder()
    b.button(text="⏹ Отмена", callback_data="madmin:catmgmt")
    await cb.message.answer(
        f"✏️ Категория «{cat.icon} {html.escape(cat.title)}» (<code>{code}</code>).\n\n"
        "Пришли новое название — можешь сразу с эмодзи, например <code>🧥 Худи</code>:\n"
        "(эмодзи в начале строки станет иконкой категории; отмена — ⏹ или /cancel)",
        reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("madmin:catdel:"))
async def madmin_cat_del(cb: CallbackQuery, session) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    code = cb.data.split(":")[2]
    repo = MerchRepository(session)
    ok = await repo.delete_category(code)
    await session.commit()
    await cb.answer("Категория удалена 🗑" if ok else "Категория не найдена 😅",
                    show_alert=True)
    await _render_admin_home(cb, session)

@router.callback_query(F.data == "madmin:addcat")
async def madmin_addcat_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    await state.set_state(MerchStates.awaiting)
    await state.update_data(step="cat_new")
    b = InlineKeyboardBuilder()
    b.button(text="⏹ Отмена", callback_data="madmin:catmgmt")
    await cb.message.answer(
        "➕ <b>Новая категория</b>\n\n"
        "Пришли название в одну строку — эмодзи сразу в нём. ID (код) сгенерируется автоматически.\n"
        "Формат: <code>ID|Название</code> или просто <code>Название</code>, например:\n"
        "<code>id1|🧥 Худи</code>  ·  <code>🎒 Сумки</code>  ·  <code>Кепки 🧢</code>\n\n"
        "Отмена — ⏹ или /cancel",
        reply_markup=b.as_markup())
    await cb.answer()

def _parse_category_input(text: str) -> tuple[str, str, str]:
    import re
    parts = [p.strip() for p in text.split("|")]
    code = ""
    body = parts[-1]
    if len(parts) > 1 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,23}", parts[0]):
        code = parts[0].lower()
    emoji = ""
    m = re.match(r"^(\S+)\s+(.*)$", body)
    if m and len(m.group(1)) <= 4 and not m.group(1).isascii():
        emoji, title = m.group(1), m.group(2).strip()
    else:
        m2 = re.match(r"^(.*?)(\s(\S+))?$", body)
        title, tail = body.strip(), (m2.group(3) if m2 else None)
        if tail and len(tail) <= 4 and not tail.isascii():
            title, emoji = body.strip()[: -len(tail)].strip(), tail
        else:
            emoji = ""
    title = title or body
    if not code:
        code = f"id{abs(hash(title.lower())) % 900 + 100}"
        code = re.sub(r"[^a-zа-я0-9_-]", "", code.lower())[:24] or f"id{secrets.randbelow(900) + 100}"
    return code, title[:64], emoji or "🧢"

@router.callback_query(F.data.startswith("madmin:cat:"))
async def madmin_cat_products(cb: CallbackQuery, session) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    code = cb.data.split(":")[2]
    repo = MerchRepository(session)
    cat = await repo.get_category(code)
    if cat is None:
        return await cb.answer("Категория не найдена 😅", show_alert=True)
    products = await repo.products(cat.id)
    b = InlineKeyboardBuilder()
    lines = [f"{cat.icon} <b>{html.escape(cat.title)}</b> — товары:", ""]
    for p in products:
        variants = await repo.variants(p.id)
        stock = sum(v.stock for v in variants)
        res = sum(1 for v in variants if v.reserved_by is not None)
        lines.append(f"• id={p.id} {html.escape(p.name)} — остаток {stock}"
                     + (f", брони {res}" if res else ""))
        b.button(text=f"⚙️ {p.name}", callback_data=f"madmin:prod:{p.id}")
        b.row()
    b.button(text="➕ Новый товар", callback_data=f"madmin:addprod:{code}")
    b.row()
    b.button(text="⬅️ К каталогу", callback_data="madmin:catalog")
    b.button(text="🏠 Управление", callback_data="madmin:home")
    if not products:
        lines.append("Товаров пока нет — добавь первый 👇")
    await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("madmin:addprod:"))
async def madmin_addprod_start(cb: CallbackQuery, session, state: FSMContext) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    code = cb.data.split(":")[2]
    repo = MerchRepository(session)
    cat = await repo.get_category(code)
    if cat is None:
        return await cb.answer("Категория не найдена 😅", show_alert=True)
    apparel = cat.code in ("hoodie", "tshirt") or "худи" in cat.title.lower() or "футбол" in cat.title.lower()
    await state.set_state(MerchStates.awaiting)
    await state.update_data(step="prod_name", cat_code=code, apparel=apparel)
    hint = "размеры S/M/L/XL/XXL × цвета Розовый/Чёрный/Белый/Серый" if apparel \
        else "без размеров и цветов (одна позиция)"
    b = InlineKeyboardBuilder()
    b.button(text="⏹ Отмена", callback_data=f"madmin:cat:{code}")
    await cb.message.answer(
        f"➕ Новый товар в «{cat.icon} {html.escape(cat.title)}» ({hint}).\n\n"
        "<b>Шаг 1/3 — название</b> принта/товара. Просто напиши его сообщением.\n"
        "Отмена — ⏹ или /cancel",
        reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("madmin:prod:"))
async def madmin_product_menu(cb: CallbackQuery, session) -> None:  # noqa: C901
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    try:
        pid = int(cb.data.split(":")[2])
    except ValueError:
        return await cb.answer()
    repo = MerchRepository(session)
    product = await repo.get_product(pid)
    if product is None:
        return await cb.answer("Товар не найден 😅", show_alert=True)
    variants = await repo.variants(pid)
    lines = [f"⚙️ <b>{html.escape(product.name)}</b> (id={pid})", ""]
    for v in variants:
        mark = " ⏳" if v.reserved_by is not None else ""
        lines.append(f"id={v.id} · {v.size or '—'}/{v.color or '—'} · "
                     f"{v.price_rub:,} ₽ · ост. {v.stock}{mark}")
    b = InlineKeyboardBuilder()
    for v in variants[:40]:
        b.button(text=f"✏️ {v.size or '—'}/{v.color or '—'}", callback_data=f"madmin:var:{v.id}")
        b.button(text=f"🗑 #{v.id}", callback_data=f"madmin:vardel:{v.id}")
        b.row()
    b.button(text="➕ Добавить позицию", callback_data=f"madmin:addvar:{pid}")
    b.row()
    b.button(text="🖼 Картинка", callback_data=f"madmin:img:{pid}")
    b.button(text="🗑 Удалить товар", callback_data=f"madmin:prodel:{pid}")
    b.row()
    from app.db.models import MerchCategory
    from sqlalchemy import select as _select
    cat = (await session.execute(
        _select(MerchCategory).where(MerchCategory.id == product.category_id)
    )).scalar_one_or_none()
    back_cb = f"madmin:cat:{cat.code}" if cat else "madmin:catalog"
    b.button(text="⬅️ Назад", callback_data=back_cb)
    b.button(text="🏠 Управление", callback_data="madmin:home")
    await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("madmin:prodel:"))
async def madmin_product_delete(cb: CallbackQuery, session) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    try:
        pid = int(cb.data.split(":")[2])
    except ValueError:
        return await cb.answer()
    repo = MerchRepository(session)
    ok = await repo.delete_product(pid)
    await session.commit()
    await cb.answer("Товар удалён 🗑" if ok else "Не найден 😅", show_alert=True)
    await madmin_catalog(cb, session)

@router.callback_query(F.data.startswith("madmin:addvar:"))
async def madmin_addvar_start(cb: CallbackQuery, session, state: FSMContext) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    parts = cb.data.split(":")
    if len(parts) > 3:
        return await madmin_addvar_pick_size(cb, session, state)
    try:
        pid = int(parts[2])
    except ValueError:
        return await cb.answer()
    repo = MerchRepository(session)
    product = await repo.get_product(pid)
    if product is None:
        return await cb.answer("Товар не найден 😅", show_alert=True)
    sizes = [sz for sz in (product.sizes or []) if sz and sz != "one"]
    if not sizes:
        await state.set_state(MerchStates.awaiting)
        await state.update_data(step="var_size", pid=pid)
        b = InlineKeyboardBuilder()
        b.button(text="⏹ Отмена", callback_data=f"madmin:prod:{pid}")
        await cb.message.answer(
            f"➕ Позиция для «{html.escape(product.name)}» — товар без размеров.\n\n"
            "<b>Шаг 1/2 — размер.</b> Напиши '-' (без размера) или новый размер сообщением.\n"
            "Отмена — ⏹ или /cancel", reply_markup=b.as_markup())
        return await cb.answer()
    b = InlineKeyboardBuilder()
    row = []
    for sz in sizes:
        row.append((sz, f"madmin:addvar:{pid}:{sz}"))
        if len(row) == 3:
            b.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
            row = []
    if row:
        b.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    b.button(text="⌨️ Другой размер…", callback_data=f"madmin:addvar:{pid}:*")
    b.row()
    b.button(text="⏹ Отмена", callback_data=f"madmin:prod:{pid}")
    await cb.message.answer(
        f"➕ Новая позиция для «{html.escape(product.name)}».\n\n"
        "<b>Шаг 1/2 — выбери размер кнопкой</b> (или введи свой через ⌨️):",
        reply_markup=b.as_markup())
    await cb.answer()

async def madmin_addvar_pick_size(cb: CallbackQuery, session, state: FSMContext) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    parts = cb.data.split(":")
    try:
        pid = int(parts[2])
    except (ValueError, IndexError):
        return await cb.answer()
    size = parts[3] if len(parts) > 3 else ""
    if size == "*":
        await state.set_state(MerchStates.awaiting)
        await state.update_data(step="var_size", pid=pid)
        b = InlineKeyboardBuilder()
        b.button(text="⏹ Отмена", callback_data=f"madmin:prod:{pid}")
        await cb.message.answer("⌨️ Введи новый размер сообщением (например M) · отмена — ⏹:",
                                reply_markup=b.as_markup())
        return await cb.answer()
    repo = MerchRepository(session)
    product = await repo.get_product(pid)
    if product is None:
        return await cb.answer("Товар не найден 😅", show_alert=True)
    colors = [c for c in (product.colors or []) if c]
    if not colors:
        price_default = min((x.price_rub for x in await repo.variants(pid)), default=0)
        v, created = await repo.add_variant(pid, size, "", price_default, 0)
        await session.commit()
        await cb.answer("Позиция добавлена ✅" if created else "Такая позиция уже есть",
                        show_alert=True)
        return await madmin_product_menu(cb, session)
    b = InlineKeyboardBuilder()
    row = []
    for c in colors:
        row.append((c, f"madmin:addcol:{pid}:{size}:{c}"))
        if len(row) == 2:
            b.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
            row = []
    if row:
        b.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    b.button(text="⌨️ Другой цвет…", callback_data=f"madmin:addcol:{pid}:{size}:*")
    b.row()
    b.button(text="⬅️ К размерам", callback_data=f"madmin:addvar:{pid}")
    b.button(text="⏹ Отмена", callback_data=f"madmin:prod:{pid}")
    await safe_edit_or_answer(
        cb.message,
        f"➕ Позиция «{html.escape(product.name)}» · размер <b>{html.escape(size)}</b>.\n\n"
        "<b>Шаг 2/2 — выбери цвет кнопкой</b> (или введи свой через ⌨️):",
        reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("madmin:addcol:"))
async def madmin_addcol_pick(cb: CallbackQuery, session, state: FSMContext) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    parts = cb.data.split(":")
    try:
        pid, size, color = int(parts[2]), parts[3], parts[4]
    except (ValueError, IndexError):
        return await cb.answer()
    repo = MerchRepository(session)
    if color == "*":
        await state.set_state(MerchStates.awaiting)
        await state.update_data(step="var_color", pid=pid, size=size)
        b = InlineKeyboardBuilder()
        b.button(text="⏹ Отмена", callback_data=f"madmin:prod:{pid}")
        await cb.message.answer(f"⌨️ Введи новый цвет для размера {size} сообщением · отмена — ⏹:",
                                reply_markup=b.as_markup())
        return await cb.answer()
    existing = await repo.find_variant(pid, size, color)
    if existing is not None:
        return await cb.answer(f"Позиция {size}/{color} уже есть (id={existing.id}) 😉",
                               show_alert=True)
    variants = await repo.variants(pid)
    price_default = min((x.price_rub for x in variants), default=0)
    v, _ = await repo.add_variant(pid, size, color, price_default, 0)
    new_vid = int(v.id)
    await session.commit()
    await cb.answer(f"✅ Позиция {size}/{color} добавлена (id={new_vid}). Задай ей остаток 👇",
                    show_alert=True)
    return await madmin_product_menu(cb, session)

@router.callback_query(F.data.startswith("madmin:var:"))
async def madmin_variant_edit(cb: CallbackQuery, session) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    try:
        vid = int(cb.data.split(":")[2])
    except ValueError:
        return await cb.answer()
    repo = MerchRepository(session)
    v = await repo.get_variant(vid)
    if v is None:
        return await cb.answer("Позиция не найдена 😅", show_alert=True)
    product = await repo.get_product(v.product_id)
    b = InlineKeyboardBuilder()
    for delta, label in ((1, "+1"), (5, "+5"), (-1, "−1"), (-5, "−5")):
        b.button(text=label, callback_data=f"madmin:stock:{vid}:{delta}")
    b.row()
    b.button(text="💳 Цена", callback_data=f"madmin:price:{vid}")
    b.button(text="🗑 Удалить позицию", callback_data=f"madmin:vardel:{vid}")
    b.row()
    b.button(text="⬅️ К товару", callback_data=f"madmin:prod:{v.product_id}")
    b.button(text="🏠 Управление", callback_data="madmin:home")
    buyer = f"\n👤 Забронирована: <code>{v.reserved_by}</code>" if v.reserved_by is not None else ""
    await safe_edit_or_answer(
        cb.message,
        f"✏️ <b>{html.escape(product.name if product else '?')}</b> · "
        f"{html.escape(v.size or '—')}/{html.escape(v.color or '—')} (id={vid})\n\n"
        f"💳 Цена: <b>{v.price_rub:,} ₽</b> · 📦 Остаток: <b>{v.stock}</b>"
        f" · Продано: {v.sold_count}{buyer}",
        reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("madmin:stock:"))
async def madmin_stock_delta(cb: CallbackQuery, session) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    parts = cb.data.split(":")
    try:
        vid, delta = int(parts[2]), int(parts[3])
    except (ValueError, IndexError):
        return await cb.answer()
    repo = MerchRepository(session)
    v = await repo.get_variant(vid)
    if v is None:
        return await cb.answer("Позиция не найдена 😅", show_alert=True)
    v.stock = max(v.stock + delta, 0)
    if v.stock <= 0:
        v.reserved_by = None
    await session.commit()
    await cb.answer(f"Остаток: {v.stock}")
    await madmin_variant_edit(cb, session)

@router.callback_query(F.data.startswith("madmin:price:"))
async def madmin_price_start(cb: CallbackQuery, session, state: FSMContext) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    try:
        vid = int(cb.data.split(":")[2])
    except ValueError:
        return await cb.answer()
    repo = MerchRepository(session)
    v = await repo.get_variant(vid)
    if v is None:
        return await cb.answer("Позиция не найдена 😅", show_alert=True)
    await state.set_state(MerchStates.awaiting)
    await state.update_data(step="var_price", vid=vid)
    await cb.message.answer(f"💳 Текущая цена: {v.price_rub:,} ₽.\n\nПришли новую цену числом (в рублях):")
    await cb.answer()

@router.callback_query(F.data.startswith("madmin:vardel:"))
async def madmin_variant_delete(cb: CallbackQuery, session) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    try:
        vid = int(cb.data.split(":")[2])
    except ValueError:
        return await cb.answer()
    repo = MerchRepository(session)
    v = await repo.get_variant(vid)
    pid = v.product_id if v else None
    ok = await repo.delete_variant(vid)
    await session.commit()
    await cb.answer("Позиция удалена 🗑" if ok else "Не найдена 😅", show_alert=True)
    if pid is not None:
        await madmin_product_menu(cb, session)

@router.callback_query(F.data.startswith("madmin:img:"))
async def madmin_img_start(cb: CallbackQuery, session, state: FSMContext) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    try:
        pid = int(cb.data.split(":")[2])
    except ValueError:
        return await cb.answer()
    repo = MerchRepository(session)
    product = await repo.get_product(pid)
    if product is None:
        return await cb.answer("Товар не найден 😅", show_alert=True)
    await state.set_state(MerchStates.awaiting)
    await state.update_data(step="prod_img", pid=pid)
    cur = product.image_url or "не задана"
    await cb.message.answer(
        f"🖼 Картинка товара «{html.escape(product.name)}»: <code>{html.escape(str(cur))}</code>\n\n"
        "Пришли URL картинки (https://...) или 'нет' чтобы убрать:")
    await cb.answer()

@router.message(MerchStates.awaiting, CommandStart())
async def merch_cancel_by_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("⏹ Ввод отменён.")

@router.message(MerchStates.awaiting, Command("cancel"))
async def merch_cancel_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("⏹ Ввод отменён.")

@router.message(MerchStates.awaiting, F.chat.type == ChatType.PRIVATE)
async def merch_admin_input(message: Message, session, state: FSMContext) -> None:
    if not _is_merch_admin(message.from_user.id):
        return await state.clear()
    data = await state.get_data()
    step = data.get("step")
    text = (message.text or "").strip()
    repo = MerchRepository(session)
    if not text:
        return await message.answer("Пустое сообщение, повтори.")
    try:
        if step == "cat_new":
            code, title, emoji = _parse_category_input(text)
            if await repo.get_category(code):
                return await message.answer(f"Категория с кодом «{code}» уже есть — пришли другой ID|Название.")
            await repo.add_category(code, title, emoji, 99)
            await session.commit()
            await state.clear()
            return await message.answer(
                f"✅ Категория «{emoji} {title}» добавлена (ID: <code>{code}</code>).",
                parse_mode="HTML")
        if step == "cat_title":
            cat = await repo.get_category(data["code"])
            if cat is None:
                await state.clear()
                return await message.answer("Категория не найдена.")
            _, new_title, new_emoji = _parse_category_input(text)
            cat.title = new_title[:64]
            cat.icon = new_emoji
            await session.commit()
            await state.clear()
            return await message.answer(f"✅ Обновлено: {new_emoji} {new_title}")
        if step == "prod_name":
            await state.update_data(step="prod_price", prod_name=text)
            return await message.answer(
                f"📝 Название: <b>{html.escape(text)}</b>\n\n"
                "<b>Шаг 2/3 — цена</b> в рублях, просто числом (например 2500):",
                parse_mode="HTML")
        if step == "prod_price":
            price = int(float(text.replace(",", "").replace(" ", "")))
            await state.update_data(step="prod_stock", price=price)
            return await message.answer(
                f"💳 Цена: <b>{price:,} ₽</b>\n\n"
                "<b>Шаг 3/3 — количество</b> на каждую позицию, числом (например 5). "
                "Для одежды будет создано 20 позиций (5 размеров × 4 цвета):",
                parse_mode="HTML")
        if step == "prod_stock":
            stock = max(int(float(text)), 0)
            code = data["cat_code"]
            cat = await repo.get_category(code)
            if cat is None:
                await state.clear()
                return await message.answer("Категория не найдена.")
            name = data["prod_name"]
            price = data["price"]
            apparel = data.get("apparel", True)
            sizes = DEFAULT_SIZES if apparel else []
            colors = DEFAULT_COLORS if apparel else []
            p = await repo.add_product(cat.id, name, f"{cat.title} с фирменным принтом канала.",
                                       sizes=sizes or ["one"], colors=colors or [""])
            combos = [(s, c) for s in (sizes or [""]) for c in (colors or [""])]
            for s, c in combos:
                await repo.add_variant(p.id, s, c, price, stock)
            new_pid = int(p.id)
            await session.commit()
            await state.clear()
            kb = InlineKeyboardBuilder()
            kb.button(text="⚙️ Открыть товар", callback_data=f"madmin:prod:{new_pid}")
            kb.button(text="📦 Каталог", callback_data="madmin:catalog")
            kb.row()
            kb.button(text="➕ Добавить ещё товар", callback_data=f"madmin:addprod:{code}")
            kb.button(text="🏠 Управление", callback_data="madmin:home")
            return await message.answer(
                f"✅ Товар «{name}» добавлен: {len(combos)} позиций · "
                f"{price:,} ₽ · остаток {stock} каждая.\n\nДальше 👇",
                reply_markup=kb.as_markup())
        if step == "var_size":
            pid = data["pid"]
            product = await repo.get_product(pid)
            if product is None:
                await state.clear()
                return await message.answer("Товар не найден.")
            has_colors = any(c for c in (product.colors or []))
            if not has_colors:
                price_default = min((x.price_rub for x in await repo.variants(pid)), default=0)
                v, created = await repo.add_variant(pid, text[:16], "", price_default, 0)
                new_vid = int(v.id)
                await session.commit()
                await state.clear()
                return await message.answer(
                    f"✅ Позиция {'добавлена' if created else 'обновлена'}: id={new_vid}. "
                    f"Задай ей остаток кнопками в меню товара.")
            await state.update_data(step="var_color", size=text[:16])
            colors = [c for c in (product.colors or []) if c]
            return await message.answer(f"Шаг 2/2 — цвет (есть: {', '.join(colors)}) или новый:")
        if step == "var_color":
            pid, size = data["pid"], data["size"]
            product = await repo.get_product(pid)
            if product is None:
                await state.clear()
                return await message.answer("Товар не найден.")
            existing = await repo.find_variant(pid, size, text)
            if existing is None:
                price_default = min((x.price_rub for x in await repo.variants(pid)), default=0)
                v, _ = await repo.add_variant(pid, size, text[:32], price_default, 0)
                new_vid = int(v.id)
                await session.commit()
                await state.clear()
                return await message.answer(
                    f"✅ Позиция {size}/{text} добавлена (id={new_vid}), цена по умолчанию "
                    f"{price_default:,} ₽, остаток 0 — поправь в меню товара.")
            await state.clear()
            return await message.answer(f"Такая позиция уже есть (id={existing.id}).")
        if step == "var_price":
            vid = data["vid"]
            v = await repo.get_variant(vid)
            if v is None:
                await state.clear()
                return await message.answer("Позиция не найдена.")
            v.price_rub = max(int(float(text.replace(",", "").replace(" ", ""))), 0)
            await session.commit()
            await state.clear()
            return await message.answer(f"✅ Цена id={vid}: {v.price_rub:,} ₽.")
        if step == "prod_img":
            pid = data["pid"]
            p = await repo.get_product(pid)
            if p is None:
                await state.clear()
                return await message.answer("Товар не найден.")
            p.image_url = None if text.lower() in ("нет", "-", "none") else text[:512]
            await session.commit()
            await state.clear()
            return await message.answer("✅ Картинка обновлена.")
    except (ValueError, IndexError):
        return await message.answer("Не понял значение (нужно число?). Попробуй ещё раз или /cancel.")
    await state.clear()
    await message.answer("Неизвестный шаг, отменил ввод.")

@router.message(F.chat.type == ChatType.PRIVATE, Command("merch"))
async def merch_admin_cmd(message: Message, session) -> None:
    if not _is_merch_admin(message.from_user.id):
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1 and args[1].strip():
        return await _merch_mutate(message, session, args[1].strip())
    repo = MerchRepository(session)
    cats = await repo.categories()
    lines = ["🧢 <b>Управление мерчем</b>", ""]
    for c in cats:
        products = await repo.products(c.id)
        lines.append(f"{c.icon} <b>{html.escape(c.title)}</b> (<code>{c.code}</code>) — {len(products)} моделей")
        for p in products:
            variants = await repo.variants(p.id)
            stock = sum(v.stock for v in variants)
            res = sum(1 for v in variants if v.reserved_by is not None)
            lines.append(f"   • id={p.id} {html.escape(p.name)} — позиций {len(variants)}, "
                         f"остаток {stock}, броней {res}")
    lines += ["", "Ниже — управление кнопками 👇 (команды тоже работают):"] + HELP_LINES
    b = _admin_kb()
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=b.as_markup())

async def _merch_mutate(message: Message, session, payload: str) -> None:
    sub, _, rest = payload.partition(" ")
    rest = rest.strip()
    repo = MerchRepository(session)
    try:
        if sub == "add_cat" and rest:
            parts = [p.strip() for p in rest.split("|")]
            code, title = parts[0], parts[1]
            if await repo.get_category(code):
                return await message.answer("Такая категория уже есть.")
            await repo.add_category(code, title, parts[2] if len(parts) > 2 else "🧢", 99)
            await session.commit()
            return await message.answer(f"✅ Категория «{title}» добавлена.")
        if sub == "del_cat" and rest:
            ok = await repo.delete_category(rest)
            await session.commit()
            return await message.answer("✅ Удалено." if ok else "Категория не найдена.")
        if sub == "add_prod" and rest:
            parts = [p.strip() for p in rest.split("|")]
            code, name = parts[0], parts[1]
            price = int(float(parts[2])) if len(parts) > 2 and parts[2] else 0
            stock = int(float(parts[3])) if len(parts) > 3 and parts[3] else 5
            cat = await repo.get_category(code)
            if cat is None:
                return await message.answer("Категория не найдена.")
            apparel = cat.code in ("hoodie", "tshirt") or "худи" in cat.title.lower() or "футбол" in cat.title.lower()
            sizes = ["S", "M", "L", "XL", "XXL"] if apparel else []
            colors = ["Розовый", "Чёрный", "Белый", "Серый"] if apparel else []
            p = await repo.add_product(cat.id, name, f"{cat.title} с фирменным принтом канала.",
                                       sizes=sizes or ["one"], colors=colors or [""])
            combos = [(s, c) for s in (sizes or [""]) for c in (colors or [""])]
            for s, c in combos:
                await repo.add_variant(p.id, s, c, price, stock)
            await session.commit()
            return await message.answer(f"✅ Товар «{name}» (id={p.id}) добавлен, позиций: {len(combos)}.")
        if sub == "add_var" and rest:
            parts = [p.strip() for p in rest.split("|")]
            pid = int(parts[0]); size = parts[1]; color = parts[2]
            price = int(float(parts[3])); stock = int(float(parts[4]))
            if await repo.get_product(pid) is None:
                return await message.answer("Товар не найден.")
            v, created = await repo.add_variant(pid, size, color, price, stock)
            await session.commit()
            verb = "добавлена" if created else "обновлена"
            return await message.answer(f"✅ Позиция {verb}: id={v.id} {size}/{color}.")
        if sub == "stock" and rest:
            parts = [p.strip() for p in rest.split("|")]
            vid = int(parts[0]); stock = max(int(float(parts[1])), 0)
            v = await repo.get_variant(vid)
            if v is None:
                return await message.answer("Позиция не найдена.")
            v.stock = stock
            if stock <= 0:
                v.reserved_by = None
            await session.commit()
            return await message.answer(f"✅ Остаток id={vid}: {stock}.")
        if sub == "del_var" and rest:
            ok = await repo.delete_variant(int(rest))
            await session.commit()
            return await message.answer("✅ Удалено." if ok else "Позиция не найдена.")
        if sub == "img" and rest:
            pid_s, _, url = rest.partition("|")
            p = await repo.get_product(int(pid_s.strip()))
            if p is None:
                return await message.answer("Товар не найден.")
            p.image_url = url.strip() or None
            await session.commit()
            return await message.answer("✅ Картинка обновлена.")
        if sub == "reserved":
            reserved = await repo.all_reserved()
            if not reserved:
                return await message.answer("Активных броней нет.")
            out = []
            for v in reserved:
                pr = await repo.get_product(v.product_id)
                out.append(f"id={v.id} · {pr.name if pr else '?'} {v.size}/{v.color} → {v.reserved_by}")
            return await message.answer("\n".join(out))
    except (ValueError, IndexError):
        return await message.answer("Неверный формат. Справка:\n" + "\n".join(HELP_LINES))
    await message.answer("Неизвестная подкоманда. Справка:\n" + "\n".join(HELP_LINES))
