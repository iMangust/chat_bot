from __future__ import annotations

import html

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ChatType
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                          InputMediaPhoto, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

from app.config import get_settings
from app.db.repositories import MerchRepository
from app.utils.safe_edit import safe_edit_or_answer







def _vsplit(b):
    if b.buttons:
        b.adjust(1)

def _vbtn(b, text, cb):
    b.row(InlineKeyboardButton(text=text, callback_data=cb))


class _VBuilder(InlineKeyboardBuilder):
    def _vb(self, text, cb):
        self.row(InlineKeyboardButton(text=text, callback_data=cb))
        return self


InlineKeyboardBuilder._vb = lambda self, text, cb: self.row(InlineKeyboardButton(text=text, callback_data=cb)) or self

router = Router(name="merch")

def _is_merch_admin(user_id: int) -> bool:
    s = get_settings()
    if user_id in (s.admin_ids or []):
        return True
    return bool(s.merch_admin_id) and user_id == s.merch_admin_id

def _media_ref(product, v) -> str | None:
    if v is not None and getattr(v, "photo_file_id", None):
        return v.photo_file_id
    return product.image_url or None

def _variant_caption(product, v) -> str:
    lines = [f"🧢 <b>{html.escape(product.name)}</b>",
             f"📏 Размер: <b>{html.escape(v.size or '—')}</b> · 🎨 {_color_label(v.color)}",
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
        b._vb(f"{c.icon} {c.title} · {len(products)} моделей", f"merch:cat:{c.code}")
        _vsplit(b)
    if not cats:
        lines.append("\nКаталог пока пуст — скоро новинки!")
    s = get_settings()
    if s.merch_url:
        b.row(InlineKeyboardButton(text="🌐 Открыть магазин мерча", url=s.merch_url))
        _vsplit(b)
    _vbtn(b, "🏠 Меню", "menu:main")
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
        b._vb(f"{cat.icon} {p.name}", f"merch:prod:{p.id}")
        _vsplit(b)
    _vbtn(b, "⬅️ К категориям", "menu:merch")
    _vbtn(b, "🏠 Меню", "menu:main")
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
            b._vb(f"{sz}{'' if has else ' ✖'}", f"merch:size:{pid}:{sz}")
        _vsplit(b)
        _vbtn(b, "⬅️ Назад", back_cb)
        _vbtn(b, "🏠 Меню", "menu:main")
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
        b._vb(f"{c}{'' if ok else ' ✖'}", f"merch:var:{v.id if v else 0}")
    _vsplit(b)
    _vbtn(b, "⬅️ К размерам", f"merch:prod:{pid}")
    _vbtn(b, "🏠 Меню", "menu:main")
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
    photo = _media_ref(product, v)
    b = InlineKeyboardBuilder()
    if v.stock > 0 and v.reserved_by is None:
        _vbtn(b, "🛒 Забронировать", f"merch:res:{v.id}")
        _vsplit(b)
    elif v.reserved_by is not None:
        text += "\n\n⏳ Эта позиция уже забронирована. Освободится после продажи или отмены брони."
    _vbtn(b, "⬅️ Назад", back_cb)
    _vbtn(b, "🏠 Меню", "menu:main")
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
    kb._vb("✅ Подтвердить продажу", f"merch:sold:{vid}")
    kb._vb("❌ Отменить резерв", f"merch:cancel:{vid}")
    _vsplit(kb)
    kb._vb("📋 Все брони", "merch:myres")
    await _notify_merch_admins(bot, admin_text, kb.as_markup())
    b = InlineKeyboardBuilder()
    _vbtn(b, "⬅️ К позиции", f"merch:var:{vid}")
    _vbtn(b, "🏠 Меню", "menu:main")
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
                f"{html.escape(v.size or '—')} · {_color_label(v.color)} — {v.price_rub:,} ₽\n\n"
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
            b._vb(f"✅ Продано #{v.id}", f"merch:sold:{v.id}")
            b._vb(f"❌ Снять #{v.id}", f"merch:cancel:{v.id}")
            _vsplit(b)
        text = "\n".join(lines)
    _vbtn(b, "⬅️ В мерч", "menu:merch")
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
COLOR_PALETTE = [
    ("Розовый", "🩷"), ("Чёрный", "🖤"), ("Белый", "🤍"), ("Серый", "🩶"),
    ("Синий", "💙"), ("Красный", "❤️"), ("Зелёный", "💚"), ("Жёлтый", "💛"),
    ("Оранжевый", "🧡"), ("Фиолетовый", "💜"), ("Коричневый", "🤎"), ("Голубой", "🩵"),
    ("Индиго", "🟪"), ("Тёмно-зелёный", "🟩"), ("Бордовый", "🟥"), ("Пудровый", "🌸"),
    ("Мятный", "🟢"), ("Золотой", "👑"), ("Серебряный", "⚡"), ("Огненный", "🔥"),
    ("Мультиколор", "🌈"), ("Хаки", "🫒"), ("Бежевый", "🐫"), ("Молочный", "🥛"),
    ("Изумрудный", "💚"), ("Лавандовый", "💐"), ("Фуксия", "🌺"), ("Салатовый", "🥬"),
    ("Горчичный", "🌭"), ("Терракотовый", "🧱"), ("Графитовый", "✒️"), ("Стальной", "⚙️"),
    ("Лазурный", "🌊"), ("Бирюзовый", "🪩"), ("Винный", "🍇"), ("Кофейный", "☕"),
    ("Кремовый", "🍦"), ("Песочный", "🏖"), ("Шоколадный", "🍫"), ("Вишнёвый", "🍒"),
    ("Коралловый", "🪸"), ("Персиковый", "🍑"), ("Лимонный", "🍋"), ("Небесный", "☁️"),
]
ALL_COLORS = [name for name, _ in COLOR_PALETTE]
DEFAULT_COLORS = ALL_COLORS[:4]
COLOR_EMOJI = {name: emo for name, emo in COLOR_PALETTE}

def _color_label(color: str | None) -> str:
    if not color:
        return "—"
    emo = COLOR_EMOJI.get(color)
    if emo is None:
        low = color.lower()
        for name, e in COLOR_PALETTE:
            if name.lower() == low or name.lower() in low or low in name.lower():
                emo = e
                break
    return f"{emo} {color}" if emo else color

def _vgrid1(b, items):
    """Вертикальная сетка с переносом длинных подписей: если текст не влезает
    в 2 колонки — кнопка на всю ширину."""
    row = []
    for text, cb in items:
        wide = len(text) > 16
        if wide and row:
            b.row(*row); row = []
        btn = InlineKeyboardButton(text=text, callback_data=cb)
        if wide:
            b.row(btn)
        else:
            row.append(btn)
            if len(row) == 2:
                b.row(*row); row = []
    if row:
        b.row(*row)

def _grid_kb(b, items, cb_fmt, cols=2):
    row = []
    for item in items:
        text, arg = (item if isinstance(item, tuple) else (item, item))
        row.append(InlineKeyboardButton(text=text, callback_data=cb_fmt.format(v=arg)))
        if len(row) == cols:
            b.row(*row)
            row = []
    if row:
        b.row(*row)

def _vgrid(b, items, cols=2):
    row = []
    for text, cb in items:
        row.append(InlineKeyboardButton(text=text, callback_data=cb))
        if len(row) == cols:
            b.row(*row)
            row = []
    if row:
        b.row(*row)

class MerchStates(StatesGroup):
    awaiting = State()

def _vrow(b):
    b.adjust(1)


def _admin_kb(extra_rows=None) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    for row in (extra_rows or []):
        for text, cb_data in row:
            _vbtn(b, text, cb_data)
        _vsplit(b)
    _vbtn(b, "📦 Каталог", "madmin:catalog")
    _vbtn(b, "📋 Брони", "merch:myres")
    _vsplit(b)
    _vbtn(b, "➕ Новая категория", "madmin:addcat")
    _vbtn(b, "➕ Новый товар", "madmin:addprod")
    _vsplit(b)
    _vbtn(b, "⬅️ В магазин", "menu:merch")
    _vbtn(b, "🏠 Меню", "menu:main")
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
        b._vb(f"{c.icon} {c.title} · {len(products)}", f"madmin:cat:{c.code}")
        _vsplit(b)
    _vbtn(b, "✏️ Изменить категории", "madmin:catmgmt")
    _vsplit(b)
    _vbtn(b, "⬅️ Назад", "madmin:home")
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
    _vbtn(b, "➕ Добавить категорию", "madmin:addcat")
    _vsplit(b)
    for c in cats:
        b._vb(f"✏️ {c.icon} {c.title}", f"madmin:catedit:{c.code}")
        _vbtn(b, "🗑", f"madmin:catdel:{c.code}")
        _vsplit(b)
    _vbtn(b, "⬅️ Назад", "madmin:catalog")
    _vbtn(b, "🏠 Управление", "madmin:home")
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
    _vbtn(b, "⏹ Отмена", "madmin:catmgmt")
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
    _vbtn(b, "⏹ Отмена", "madmin:catmgmt")
    await cb.message.answer(
        "➕ <b>Новая категория</b>\n\n"
        "Просто пришли название в одну строку — эмодзи сразу в нём, например:\n"
        "<code>🎒 Сумки</code>  ·  <code>Кепки 🧢</code>\n\n"
        "ID (например id4) бот выдаст сам — по порядку.\n"
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
    return code, title[:64], emoji or "🧢"

async def _next_category_code(repo: MerchRepository) -> str:
    n = 1
    while await repo.get_category(f"id{n}") is not None:
        n += 1
    return f"id{n}"

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
        b._vb(f"⚙️ {p.name}", f"madmin:prod:{p.id}")
        _vsplit(b)
    _vbtn(b, "➕ Новый товар", f"madmin:addprod:{code}")
    _vsplit(b)
    _vbtn(b, "⬅️ К каталогу", "madmin:catalog")
    _vbtn(b, "🏠 Управление", "madmin:home")
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
    await state.set_state(MerchStates.awaiting)
    await state.update_data(step="prod_name", cat_code=code)
    b = InlineKeyboardBuilder()
    _vbtn(b, "⏹ Отмена", f"madmin:cat:{code}")
    await cb.message.answer(
        f"➕ Новый товар в «{cat.icon} {html.escape(cat.title)}».\n\n"
        "<b>Шаг 1 из 6 — название.</b> Просто напиши его сообщением.\n"
        "Дальше: цена → размер → цвет → фото → количество (для каждой позиции своё).\n"
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
        b._vb(f"✏️ {v.size or '—'}/{v.color or '—'} · ост. {v.stock}", f"madmin:var:{v.id}")
        _vsplit(b)
    _vbtn(b, "➕ Добавить позицию", f"madmin:addvar:{pid}")
    _vsplit(b)
    _vbtn(b, "🖼 Фото товара", f"madmin:img:{pid}")
    _vbtn(b, "🗑 Удалить товар", f"madmin:prodel:{pid}")
    _vsplit(b)
    from app.db.models import MerchCategory
    from sqlalchemy import select as _select
    cat = (await session.execute(
        _select(MerchCategory).where(MerchCategory.id == product.category_id)
    )).scalar_one_or_none()
    back_cb = f"madmin:cat:{cat.code}" if cat else "madmin:catalog"
    _vbtn(b, "⬅️ Назад", back_cb)
    _vbtn(b, "🏠 Управление", "madmin:home")
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
    try:
        pid = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        return await cb.answer()
    repo = MerchRepository(session)
    product = await repo.get_product(pid)
    if product is None:
        return await cb.answer("Товар не найден 😅", show_alert=True)
    await state.set_state(MerchStates.awaiting)
    await state.update_data(step="mv_size", pid=pid)
    sizes = [s for s in (product.sizes or []) if s and s != "one"]
    b = InlineKeyboardBuilder()
    _vgrid(b, [(sz, f"madmin:mvsize:{pid}:{sz}") for sz in sizes], cols=3)
    _vbtn(b, "🚫 Без размеров (one size)", f"madmin:mvsize:{pid}:nosize")
    _vsplit(b)
    _vbtn(b, "⌨️ Другой размер…", f"madmin:mvsize:{pid}:*")
    _vsplit(b)
    _vbtn(b, "⬅️ К товару", f"madmin:prod:{pid}")
    _vbtn(b, "🏠 Управление", "madmin:home")
    await safe_edit_or_answer(
        cb.message,
        f"➕ Новая позиция «{html.escape(product.name)}»\n\n"
        "<b>Шаг 3 из 6 — размер.</b> Нажми кнопку 👇",
        reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("madmin:mvsize:"))
async def madmin_mv_size(cb: CallbackQuery, session, state: FSMContext) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    parts = cb.data.split(":")
    try:
        pid = int(parts[2])
    except (ValueError, IndexError):
        return await cb.answer()
    size = parts[3] if len(parts) > 3 else ""
    if size.startswith("-") and size[1:].isdigit():
        size = size[1:]
    elif len(parts) > 4 and parts[4].lstrip("-").isdigit():
        size = size[:-1]
    if size == "nosize":
        return await _wizard_after_color(cb, session, state, pid, "", "")
    if size == "*":
        await state.set_state(MerchStates.awaiting)
        await state.update_data(step="mv_size_text", pid=pid)
        b = InlineKeyboardBuilder()
        _vbtn(b, "⬅️ К размерам", f"madmin:addvar:{pid}")
        _vbtn(b, "🏠 Управление", "madmin:home")
        await cb.message.answer("⌨️ Напиши новый размер сообщением (например M):",
                                reply_markup=b.as_markup())
        return await cb.answer()
    repo = MerchRepository(session)
    product = await repo.get_product(pid)
    if product is None:
        return await cb.answer("Товар не найден 😅", show_alert=True)
    colors = [c for c in (product.colors or ALL_COLORS) if c]
    try:
        page = int(parts[4]) if len(parts) > 5 else 0
    except ValueError:
        page = 0
    per_page = 10
    pages = max(1, (len(colors) + per_page - 1) // per_page)
    page %= pages
    chunk = colors[page * per_page:(page + 1) * per_page]
    b = InlineKeyboardBuilder()
    _vgrid1(b, [(_color_label(c), f"madmin:mvcolor:{pid}:{size}:{c}") for c in chunk])
    nav = []
    if page > 0:
        nav.append(("◀️", f"madmin:mvsize:{pid}:{size}:-{page - 1}"))
    nav.append((f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(("▶️", f"madmin:mvsize:{pid}:{size}:-{page + 1}"))
    b.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in nav])
    _vbtn(b, "⌨️ Другой цвет…", f"madmin:mvcolor:{pid}:{size}:*")
    _vsplit(b)
    _vbtn(b, "⬅️ К размерам", f"madmin:addvar:{pid}")
    _vbtn(b, "🏠 Управление", "madmin:home")
    await safe_edit_or_answer(
        cb.message,
        f"➕ «{html.escape(product.name)}» · размер <b>{html.escape(size)}</b>\n\n"
        "<b>Шаг 4 из 6 — цвет.</b> Нажми кнопку 👇",
        reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("madmin:mvcolor:"))
async def madmin_mv_color(cb: CallbackQuery, session, state: FSMContext) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    parts = cb.data.split(":")
    try:
        pid, size = int(parts[2]), parts[3]
        color = ":".join(parts[4:])
    except (ValueError, IndexError):
        return await cb.answer()
    if color == "*":
        await state.set_state(MerchStates.awaiting)
        await state.update_data(step="mv_color_text", pid=pid, size=size)
        b = InlineKeyboardBuilder()
        _vbtn(b, "⬅️ К цветам", f"madmin:mvsize:{pid}:{size}")
        _vbtn(b, "🏠 Управление", "madmin:home")
        await cb.message.answer(f"⌨️ Напиши новый цвет для размера {size} сообщением:",
                                reply_markup=b.as_markup())
        return await cb.answer()
    repo = MerchRepository(session)
    existing = await repo.find_variant(pid, size, color)
    if existing is not None:
        return await cb.answer(f"Позиция {size}/{color} уже есть (id={existing.id}) 😉",
                               show_alert=True)
    return await _wizard_after_color(cb, session, state, pid, size, color)

async def _wizard_after_color(cb: CallbackQuery, session, state: FSMContext,
                              pid: int, size: str, color: str) -> None:
    await state.set_state(MerchStates.awaiting)
    await state.update_data(step="mv_photo", pid=pid, size=size, color=color)
    b = InlineKeyboardBuilder()
    _vbtn(b, "📷 Пришли фото сообщением", f"madmin:mvphotopick:{pid}")
    _vsplit(b)
    _vbtn(b, "⏭ Без фото — к цене", f"madmin:mvspeed:{pid}")
    _vbtn(b, "⬅️ К цветам", f"madmin:mvsize:{pid}:{size}")
    _vsplit(b)
    _vbtn(b, "⚙️ К товару", f"madmin:prod:{pid}")
    _vbtn(b, "🏠 Управление", "madmin:home")
    await safe_edit_or_answer(
        cb.message,
        f"➕ Позиция <b>{html.escape(size or '—')}/{html.escape(color or '—')}</b>.\n\n"
        "<b>Шаг 5 из 6 — фото.</b>\n"
        "Нажми «📷» и пришли картинку — она сохранится навсегда. Или 👇",
        reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("madmin:mvphotopick:"))
async def madmin_mv_photopick(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    data = await state.get_data()
    if data.get("step") not in ("mv_photo", "mv_photo_attach"):
        return await cb.answer()
    await state.set_state(MerchStates.awaiting)
    data["step"] = "mv_photo_attach"
    await state.set_data(data)
    pid = data["pid"]
    b = InlineKeyboardBuilder()
    _vbtn(b, "⏭ Пропустить — без фото", f"madmin:mvspeed:{pid}")
    _vbtn(b, "⬅️ Назад", f"madmin:addvar:{pid}")
    _vsplit(b)
    _vbtn(b, "🏠 Управление", "madmin:home")
    await cb.message.answer("📷 Пришли фото позиции сообщением (или пропуск 👇):",
                            reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("madmin:mvspeed:"))
async def madmin_mv_price(cb: CallbackQuery, session, state: FSMContext) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    data = await state.get_data()
    if data.get("step") not in ("mv_photo", "mv_photo_attach"):
        return await cb.answer()
    await state.set_state(MerchStates.awaiting)
    data["step"] = "mv_price"
    await state.set_data(data)
    b = InlineKeyboardBuilder()
    _vbtn(b, "⬅️ К фото", f"madmin:addvar:{data['pid']}")
    _vbtn(b, "🏠 Управление", "madmin:home")
    await safe_edit_or_answer(
        cb.message,
        "<b>Шаг 6 из 6 — количество штук.</b> Напиши числом (например 10):",
        reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("madmin:vphoto:"))
async def madmin_vphoto_later(cb: CallbackQuery, session, state: FSMContext) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    try:
        vid = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        return await cb.answer()
    repo = MerchRepository(session)
    v = await repo.get_variant(vid)
    if v is None:
        return await cb.answer("Позиция не найдена 😅", show_alert=True)
    await state.set_state(MerchStates.awaiting)
    await state.update_data(step="var_photo", vid=vid)
    b = InlineKeyboardBuilder()
    _vbtn(b, "✏️ К позиции", f"madmin:var:{vid}")
    _vbtn(b, "🏠 Управление", "madmin:home")
    await cb.message.answer("📷 Пришли фото позиции сообщением — сохраню навсегда:",
                            reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("madmin:vphotodel:"))
async def madmin_vphoto_delete(cb: CallbackQuery, session) -> None:
    if not _is_merch_admin(cb.from_user.id):
        return await cb.answer("Только для админов мерча 🙅", show_alert=True)
    try:
        vid = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        return await cb.answer()
    repo = MerchRepository(session)
    v = await repo.get_variant(vid)
    if v is None:
        return await cb.answer("Позиция не найдена 😅", show_alert=True)
    v.photo_file_id = None
    await session.commit()
    await cb.answer("Фото убрано 🖼")
    await madmin_variant_edit(cb, session)

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
        b._vb(label, f"madmin:stock:{vid}:{delta}")
    _vsplit(b)
    _vbtn(b, "💳 Цена", f"madmin:price:{vid}")
    if v.photo_file_id:
        _vbtn(b, "🖼 Убрать фото", f"madmin:vphotodel:{vid}")
    else:
        _vbtn(b, "📷 Добавить фото", f"madmin:vphoto:{vid}")
    _vsplit(b)
    _vbtn(b, "🗑 Удалить позицию", f"madmin:vardel:{vid}")
    _vsplit(b)
    _vbtn(b, "⬅️ К товару", f"madmin:prod:{v.product_id}")
    _vbtn(b, "🏠 Управление", "madmin:home")
    buyer = f"\n👤 Забронирована: <code>{v.reserved_by}</code>" if v.reserved_by is not None else ""
    await safe_edit_or_answer(
        cb.message,
        f"✏️ <b>{html.escape(product.name if product else '?')}</b> · "
        f"{html.escape(v.size or '—')}/{_color_label(v.color)} (id={vid})\n\n"
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

async def _save_photo_as_file_id(bot: Bot, message: Message) -> str | None:
    if not message.photo:
        return None
    fid = message.photo[-1].file_id
    try:
        up = await bot.send_photo(message.chat.id, fid)
        saved = up.photo[-1].file_id if up.photo else fid
        await bot.delete_message(message.chat.id, up.message_id)
        return saved
    except Exception as exc:
        logger.warning("merch photo re-save failed: {}: {}", type(exc).__name__, exc)
        return fid

_PHOTO_STEPS = {"mv_photo_attach", "var_photo"}

@router.message(MerchStates.awaiting, F.photo & F.chat.type == ChatType.PRIVATE)
async def merch_admin_photo_input(message: Message, session, state: FSMContext) -> None:
    if not _is_merch_admin(message.from_user.id):
        return await state.clear()
    data = await state.get_data()
    step = data.get("step")
    if step not in _PHOTO_STEPS:
        return
    repo = MerchRepository(session)
    fid = await _save_photo_as_file_id(message.bot, message)
    if not fid:
        b = InlineKeyboardBuilder()
        _vbtn(b, "📷 Повторить загрузку", "noop")
        if step == "mv_photo_attach":
            _vbtn(b, "⏭ Пропустить без фото", f"madmin:mvspeed:{data.get('pid', 0)}")
        elif step == "var_photo":
            _vbtn(b, "✏️ К позиции", f"madmin:var:{data.get('vid', 0)}")
        _vbtn(b, "❌ Отменить", "madmin:home")
        return await message.answer(
            "Не удалось сохранить фото 😅 Telegram не отдал файл (часто бывает со старыми пересланными "
            "картинками). Пришли фото ещё раз обычным сообщением — обычно помогает.\n"
            "Или пропусти фото / отмени ввод 👇", reply_markup=b.as_markup())
    if step == "var_photo":
        vid = data["vid"]
        v = await repo.get_variant(vid)
        if v is None:
            await state.clear()
            return await message.answer("Позиция не найдена.")
        v.photo_file_id = fid
        await session.commit()
        await state.clear()
        kb = InlineKeyboardBuilder()
        kb._vb("✏️ К позиции", f"madmin:var:{vid}")
        kb._vb("🏠 Управление", "madmin:home")
        return await message.answer("✅ Фото позиции сохранено — покупатель увидит его на витрине!",
                                    reply_markup=kb.as_markup())
    pid = data["pid"]
    v = await repo.find_variant(pid, data.get("size", ""), data.get("color", ""))
    if v is None:
        variants = await repo.variants(pid)
        v = variants[-1] if variants else None
    if v is not None:
        v.photo_file_id = fid
        await session.commit()
    await state.update_data(step="mv_price")
    kb = InlineKeyboardBuilder()
    kb._vb("⬅️ К фото", f"madmin:addvar:{pid}")
    kb._vb("🏠 Управление", "madmin:home")
    return await message.answer("✅ Фото принято!\n\n<b>Шаг 6 из 6 — количество штук.</b> Напиши числом (например 10):",
                                parse_mode="HTML", reply_markup=kb.as_markup())

@router.message(MerchStates.awaiting, CommandStart())
async def merch_cancel_by_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("⏹ Ввод отменён.")

@router.message(MerchStates.awaiting, Command("cancel"))
async def merch_cancel_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("⏹ Ввод отменён.")

@router.message(MerchStates.awaiting, F.sticker | F.video | F.document | F.audio | F.voice | F.animation | F.video_note)
async def merch_admin_wrong_media(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("step") in _PHOTO_STEPS:
        return await message.answer("Это не фото 😅 Пришли обычную картинку сообщением или пропусти шаг кнопкой.")
    return await message.answer("Нужен текстовый ответ 🙂 Напиши значение сообщением.")

@router.message(MerchStates.awaiting, F.chat.type == ChatType.PRIVATE)
async def merch_admin_input(message: Message, session, state: FSMContext) -> None:
    if not _is_merch_admin(message.from_user.id):
        return await state.clear()
    data = await state.get_data()
    step = data.get("step")
    text = (message.text or "").strip()
    repo = MerchRepository(session)
    if not text:
        if step in _PHOTO_STEPS:
            return await message.answer("📷 Пришли фото позицией (картинкой), «-» чтобы пропустить, или отмени /cancel.")
        return await message.answer("Пустое сообщение, повтори.")
    if step in _PHOTO_STEPS and text.lower() in ("-", "нет", "пропустить", "skip"):
        if step == "var_photo":
            await state.clear()
            kb = InlineKeyboardBuilder()
            kb._vb("✏️ К позиции", f"madmin:var:{data['vid']}")
            kb._vb("🏠 Управление", "madmin:home")
            return await message.answer("⏭ Фото пропущено.", reply_markup=kb.as_markup())
        await state.update_data(step="mv_price")
        kb = InlineKeyboardBuilder()
        kb._vb("⬅️ К фото", f"madmin:addvar:{data['pid']}")
        kb._vb("🏠 Управление", "madmin:home")
        return await message.answer("<b>Шаг 6 из 6 — количество штук.</b> Напиши числом (например 10):",
                                    parse_mode="HTML", reply_markup=kb.as_markup())
    try:
        if step == "cat_new":
            code, title, emoji = _parse_category_input(text)
            if code:
                if await repo.get_category(code):
                    return await message.answer(f"Категория с кодом «{code}» уже есть — выбери другой ID или пришли просто название.")
            else:
                code = await _next_category_code(repo)
            cats = await repo.categories()
            await repo.add_category(code, title, emoji, len(cats))
            await session.commit()
            await state.clear()
            kb = InlineKeyboardBuilder()
            kb._vb("➕ Добавить ещё категорию", "madmin:addcat")
            kb._vb("✏️ Изменить категории", "madmin:catmgmt")
            _vsplit(kb)
            kb._vb("📦 Каталог", "madmin:catalog")
            kb._vb("🏠 Управление", "madmin:home")
            return await message.answer(
                f"✅ Категория «{emoji} {title}» добавлена (ID: <code>{code}</code>).\n\nДальше 👇",
                parse_mode="HTML", reply_markup=kb.as_markup())
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
            await state.update_data(step="prod_price", prod_name=text[:80])
            kb = InlineKeyboardBuilder()
            kb._vb("⏹ Отмена", f"madmin:cat:{data['cat_code']}")
            return await message.answer(
                f"✅ Название: <b>{html.escape(text)}</b>\n\n"
                "<b>Шаг 2 из 6 — цена</b> в рублях. Просто напиши числом (например 2500):\n"
                "Отмена — кнопка ниже или /cancel",
                parse_mode="HTML", reply_markup=kb.as_markup())
        if step == "prod_price":
            try:
                price = int(float(text.replace(",", "").replace(" ", "").replace("₽", "")))
            except ValueError:
                return await message.answer("Напиши цену числом, например 2500 🙂")
            code = data["cat_code"]
            cat = await repo.get_category(code)
            if cat is None:
                await state.clear()
                return await message.answer("Категория не найдена.")
            name = data["prod_name"]
            p = await repo.add_product(cat.id, name, f"{cat.title} с фирменным принтом канала.",
                                       sizes=list(DEFAULT_SIZES), colors=ALL_COLORS)
            new_pid = int(p.id)
            await session.commit()
            await state.update_data(step="mv_size", pid=new_pid)
            kb = InlineKeyboardBuilder()
            _vgrid(kb, [(sz, f"madmin:mvsize:{new_pid}:{sz}") for sz in DEFAULT_SIZES], cols=3)
            kb._vb("🚫 Без размеров (one size)", f"madmin:mvsize:{new_pid}:nosize")
            _vsplit(kb)
            kb._vb("⌨️ Другой размер…", f"madmin:mvsize:{new_pid}:*")
            _vsplit(kb)
            kb._vb("⬅️ К категории", f"madmin:cat:{code}")
            kb._vb("🏠 Управление", "madmin:home")
            return await message.answer(
                f"✅ Создан товар <b>{html.escape(name)}</b> · 💳 <b>{price:,} ₽</b> (общая цена).\n\n"
                "<b>Шаг 3 из 6 — размер позиции.</b> Нажми кнопку 👇\n"
                "Позиции создаются по одной: сам решаешь, сколько и каких размеров/цветов нужно.",
                parse_mode="HTML", reply_markup=kb.as_markup())
        if step == "mv_size_text":
            pid = data["pid"]
            await state.update_data(step="mv_photo", size=text[:16], color="")
            repo2 = MerchRepository(session)
            await repo2.add_variant(pid, text[:16], "", 0, 0)
            await session.commit()
            b = InlineKeyboardBuilder()
            _vbtn(b, "📷 Прикрепить фото этой позиции", f"madmin:mvphotopick:{pid}")
            _vsplit(b)
            _vbtn(b, "⏭ Без фото", f"madmin:mvspeed:{pid}")
            _vbtn(b, "⏹ Отмена", f"madmin:prod:{pid}")
            return await message.answer(
                f"➕ Позиция <b>{html.escape(text[:16])}/—</b>.\n\n<b>Шаг 5 из 6 — фото.</b>",
                parse_mode="HTML", reply_markup=b.as_markup())
        if step == "mv_color_text":
            pid, size = data["pid"], data["size"]
            existing = await repo.find_variant(pid, size, text[:32])
            if existing is not None:
                await state.clear()
                return await message.answer(f"Такая позиция уже есть (id={existing.id}).")
            await repo.add_variant(pid, size, text[:32], 0, 0)
            await session.commit()
            await state.update_data(step="mv_photo", color=text[:32])
            b = InlineKeyboardBuilder()
            _vbtn(b, "📷 Прикрепить фото этой позиции", f"madmin:mvphotopick:{pid}")
            _vsplit(b)
            _vbtn(b, "⏭ Без фото", f"madmin:mvspeed:{pid}")
            _vbtn(b, "⏹ Отмена", f"madmin:prod:{pid}")
            return await message.answer(
                f"➕ Позиция <b>{html.escape(size)}/{html.escape(text[:32])}</b>.\n\n<b>Шаг 5 из 6 — фото.</b>",
                parse_mode="HTML", reply_markup=b.as_markup())
        if step == "mv_price":
            pid = data["pid"]
            price = max(int(float(text.replace(",", "").replace(" ", ""))), 0)
            v = await repo.find_variant(pid, data.get("size", ""), data.get("color", ""))
            if v is None:
                await state.clear()
                return await message.answer("Позиция не найдена — начни заново.")
            v.price_rub = price
            vid = int(v.id)
            await session.commit()
            await state.clear()
            kb = InlineKeyboardBuilder()
            kb._vb("📦 Задать остаток (±1/±5)", f"madmin:var:{vid}")
            kb._vb("🖼 Добавить фото позже", f"madmin:vphoto:{vid}")
            _vsplit(kb)
            kb._vb("⚙️ Открыть товар", f"madmin:prod:{pid}")
            kb._vb("➕ Добавить ещё позицию", f"madmin:addvar:{pid}")
            _vsplit(kb)
            kb._vb("📦 Каталог", "madmin:catalog")
            kb._vb("🏠 Управление", "madmin:home")
            return await message.answer(
                f"✅ Позиция готова: {html.escape(data.get('size') or '—')}/{html.escape(data.get('color') or '—')} "
                f"— <b>{price:,} ₽</b> (остаток 0 — задай кнопками 👇).\n\nДальше 👇",
                parse_mode="HTML", reply_markup=kb.as_markup())
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
            fid = await _save_photo_as_file_id(message.bot, message)
            if fid:
                p.image_url = fid
            else:
                p.image_url = None if text.lower() in ("нет", "-", "none") else text[:512]
            await session.commit()
            await state.clear()
            kb = InlineKeyboardBuilder()
            kb._vb("⚙️ Открыть товар", f"madmin:prod:{pid}")
            kb._vb("📦 Каталог", "madmin:catalog")
            return await message.answer("✅ Картинка сохранена!\n\nДальше 👇",
                                        reply_markup=kb.as_markup())
        if step == "var_photo":
            vid = data["vid"]
            v = await repo.get_variant(vid)
            if v is None:
                await state.clear()
                return await message.answer("Позиция не найдена.")
            fid = await _save_photo_as_file_id(message.bot, message)
            if not fid:
                return await message.answer("Это не фото 😅 Пришли картинку сообщением или отмени /cancel.")
            v.photo_file_id = fid
            await session.commit()
            await state.clear()
            kb = InlineKeyboardBuilder()
            kb._vb("✏️ К позиции", f"madmin:var:{vid}")
            kb._vb("🏠 Управление", "madmin:home")
            return await message.answer("✅ Фото позиции сохранено — покупатель увидит его на витрине!",
                                        reply_markup=kb.as_markup())
        if step == "mv_photo_attach":
            pid = data["pid"]
            fid = await _save_photo_as_file_id(message.bot, message)
            if not fid:
                return await message.answer("Это не фото 😅 Пришли картинку или нажми ⏭ Без фото позже; отмена /cancel.")
            v = await repo.find_variant(pid, data.get("size", ""), data.get("color", ""))
            if v is None:
                variants = await repo.variants(pid)
                v = variants[-1] if variants else None
            if v is not None:
                v.photo_file_id = fid
                await session.commit()
            await state.update_data(step="mv_price")
            return await message.answer("✅ Фото принято!\n\n<b>Шаг 4 из 4 — цена</b> в рублях, числом:",
                                        parse_mode="HTML")
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
            apparel = any(w in cat.title.lower() for w in ("худи", "худ", "футбол", "лонгслив", "кенгуру", "свитшот"))
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
