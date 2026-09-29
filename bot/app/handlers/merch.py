from __future__ import annotations

import html

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.enums import ChatType
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InputMediaPhoto,
                           Message)
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
    lines += [""] + HELP_LINES
    await message.answer("\n".join(lines), parse_mode="HTML")

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
