from __future__ import annotations

import html
import re

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InputMediaPhoto, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import get_settings
from app.db.repositories import EventRepository
from app.utils.local_time import now as local_now
from app.utils.safe_edit import safe_edit_or_answer







def _vrow(b):
    b._markup = [list([btn]) for btn in list(b.buttons)]
    b.max_width = 1


router = Router(name="events")

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


def _ru_date(iso: str) -> str:
    try:
        y, m, d = (int(p) for p in str(iso).split("-"))
        return f"{d} {MONTHS[m - 1]}"
    except Exception:
        return iso or "?"


def _is_event_admin(user_id: int) -> bool:
    s = get_settings()
    if user_id in (s.admin_ids or []):
        return True
    return bool(s.merch_admin_id) and user_id == s.merch_admin_id


class EvStates(StatesGroup):
    awaiting = State()


FIELDS = [
    ("title", "Название"),
    ("date", "Дата"),
    ("time", "Время"),
    ("place", "Место"),
    ("meet", "Сбор"),
    ("description", "Описание"),
    ("image_url", "Афиша (URL)"),
    ("url", "Ссылка (билеты)"),
]

PROMPTS = {
    "title": "Например: Концерт Кани Уэста",
    "date": "Формат: 1 октября, 01.10, 1 окт или 2026-10-01",
    "time": "Например: 19:00 (можно «-»)",
    "place": "Например: клуб «Питон», пр. Ленина 12",
    "meet": "Например: сбор 18:30 у входа",
    "description": "Подробности: программа, цена билета, что взять с собой",
    "image_url": "Прямая ссылка http(s)://… на афишу",
    "url": "Прямая ссылка http(s)://… (билеты/подробности)",
}

ADD_STEPS = ["title", "date", "time", "place", "meet", "description"]


def _summary(ev) -> str:
    lines = [f"{ev.icon} <b>{html.escape(ev.title)}</b>"]
    if ev.date:
        when = _ru_date(ev.date) + (f" · ⏰ {html.escape(ev.time)}" if ev.time else "")
        lines.append(f"🗓 {when}")
    if ev.place:
        lines.append(f"📍 {html.escape(ev.place)}")
    if ev.meet:
        lines.append(f"🚩 Сбор: {html.escape(ev.meet)}")
    if ev.description:
        lines.append(f"\n{html.escape(ev.description)}")
    going = len(ev.going or [])
    if going:
        lines.append(f"\n🙋 Пойдут: {going}")
    return "\n".join(lines)


def _parse_date(raw: str) -> str | None:
    raw = raw.strip().lower().rstrip(".")
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", raw)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{1,2})[.](\d{1,2})(?:[.](\d{2,4}))?$", raw)
    if m:
        y = local_now().year
        if m.group(3):
            yy = int(m.group(3))
            y = 2000 + yy if yy < 100 else yy
        return f"{y:04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.match(r"^(\d{1,2})\s*([а-яa-z]+)\.?$", raw)
    if m:
        day = int(m.group(1))
        mon = m.group(2)[:3]
        idx = None
        for i, name in enumerate(MONTHS):
            if name.startswith(mon):
                idx = i + 1
                break
        if idx is None:
            short = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5,
                     "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10,
                     "ноя": 11, "дек": 12}
            idx = short.get(mon)
        if idx:
            y = local_now().year
            cand = f"{y:04d}-{idx:02d}-{day:02d}"
            if cand < local_now().date().isoformat():
                cand = f"{y + 1:04d}-{idx:02d}-{day:02d}"
            return cand
    return None


async def _render_list(cb: CallbackQuery, session) -> None:
    repo = EventRepository(session)
    all_ev = await repo.all()
    today = local_now().date().isoformat()
    events = [e for e in all_ev if (e.date or "9999") >= today]
    past = [e for e in all_ev if (e.date or "9999") < today][-5:]
    b = InlineKeyboardBuilder()
    if not events and not past:
        text = ("<b>📅 Мероприятия канала</b>\n\n"
                "Пока пусто 🎪\n"
                "Скоро тут появятся анонсы концертов, выездов и встреч — "
                "следи за новостями в канале!")
    else:
        lines = ["<b>📅 Мероприятия канала</b>", ""]
        for e in events[:10]:
            when = _ru_date(e.date) + (f", {e.time}" if e.time else "")
            place = f" · {html.escape(e.place)}" if e.place else ""
            lines.append(f"{e.icon} <b>{when}</b> — {html.escape(e.title)}{place}")
        text = "\n".join(lines)
        for e in events[:10]:
            b.button(text=f"{e.icon} {_ru_date(e.date)} — {e.title}",
                     callback_data=f"ev:view:{e.id}")
            _vrow(b)
        if past:
            for e in reversed(past):
                b.button(text=f"✔️ Прошло: {e.title}", callback_data=f"ev:view:{e.id}")
                _vrow(b)
    if _is_event_admin(cb.from_user.id):
        b.button(text="🛠 Управление мероприятиями", callback_data="evadmin:home")
        _vrow(b)
    b.button(text="🏠 Меню", callback_data="menu:main")
    await safe_edit_or_answer(cb.message, text, reply_markup=b.as_markup())


@router.callback_query(F.data == "menu:events")
async def menu_events(cb: CallbackQuery, session) -> None:
    await _render_list(cb, session)
    await cb.answer()

@router.callback_query(CommandStart("menu"))
async def menu_any_unhandled(cb: CallbackQuery) -> None:
    if cb.data and cb.data.count(":") >= 2:
        try:
            await cb.message.delete_reply_markup()
        except Exception:
            pass
    await cb.answer("Обнови меню: напиши /start 🙂", show_alert=True)


async def _detail_render(cb: CallbackQuery, session, eid: int) -> None:
    repo = EventRepository(session)
    ev = await repo.get(eid)
    if ev is None:
        await _render_list(cb, session)
        return
    going = list(ev.going or [])
    joined = cb.from_user.id in going
    text = _summary(ev)
    b = InlineKeyboardBuilder()
    if ev.url:
        b.button(text="🔗 Подробнее / билеты", url=ev.url)
        _vrow(b)
    mark = "↩️ Отменить участие" if joined else "✅ Я пойду!"
    b.button(text=f"{mark} ({len(going)})" if going else mark,
             callback_data=f"ev:going:{eid}")
    _vrow(b)
    b.button(text="⬅️ К списку", callback_data="menu:events")
    b.button(text="🏠 Меню", callback_data="menu:main")
    _vrow(b)
    if _is_event_admin(cb.from_user.id):
        b.button(text="✏️ Редактировать", callback_data=f"evadmin:item:{eid}")
        _vrow(b)
    msg = cb.message
    try:
        if ev.image_url:
            await msg.edit_media(
                media=InputMediaPhoto(media=ev.image_url, caption=text,
                                      parse_mode="HTML"),
                reply_markup=b.as_markup())
        else:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=b.as_markup())
    except Exception:
        await safe_edit_or_answer(msg, text, reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("ev:view:"))
async def event_detail(cb: CallbackQuery, session) -> None:
    try:
        eid = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer("Некорректное мероприятие.", show_alert=True)
        return
    await _detail_render(cb, session, eid)
    await cb.answer()


@router.callback_query(F.data.startswith("ev:going:"))
async def event_going(cb: CallbackQuery, session) -> None:
    try:
        eid = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer()
        return
    repo = EventRepository(session)
    joined, count = await repo.toggle_going(eid, cb.from_user.id)
    await session.commit()
    await cb.answer("Записали тебя! 🙌" if joined else "Участие отменено.")
    await _detail_render(cb, session, eid)


async def _admin_home(cb: CallbackQuery, session) -> None:
    if not _is_event_admin(cb.from_user.id):
        await cb.answer("Только для админов.", show_alert=True)
        return
    repo = EventRepository(session)
    all_ev = await repo.all()
    today = local_now().date().isoformat()
    upcoming = [e for e in all_ev if (e.date or "9999") >= today]
    text = (f"🛠 <b>Управление мероприятиями</b>\n\n"
            f"Предстоящих: <b>{len(upcoming)}</b> · всего в базе: <b>{len(all_ev)}</b>")
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить мероприятие", callback_data="evadmin:add")
    _vrow(b)
    b.button(text="📋 Все мероприятия", callback_data="evadmin:list")
    _vrow(b)
    b.button(text="⬅️ Назад к списку", callback_data="menu:events")
    b.button(text="🏠 Меню", callback_data="menu:main")
    _vrow(b)
    await safe_edit_or_answer(cb.message, text, reply_markup=b.as_markup())


@router.callback_query(F.data == "evadmin:home")
async def evadmin_home(cb: CallbackQuery, session) -> None:
    await _admin_home(cb, session)
    await cb.answer()


async def _admin_list(cb: CallbackQuery, session) -> None:
    if not _is_event_admin(cb.from_user.id):
        await cb.answer("Только для админов.", show_alert=True)
        return
    repo = EventRepository(session)
    all_ev = await repo.all()
    b = InlineKeyboardBuilder()
    if not all_ev:
        text = "📋 В базе пока нет мероприятий."
    else:
        text = "📋 <b>Все мероприятия</b> (нажми, чтобы управлять):"
        for e in all_ev[-30:]:
            b.button(text=f"{e.icon} {e.date or '?'} — {e.title}",
                     callback_data=f"evadmin:item:{e.id}")
            _vrow(b)
    b.button(text="➕ Добавить", callback_data="evadmin:add")
    _vrow(b)
    b.button(text="⬅️ Назад", callback_data="evadmin:home")
    await safe_edit_or_answer(cb.message, text, reply_markup=b.as_markup())


@router.callback_query(F.data == "evadmin:list")
async def evadmin_list(cb: CallbackQuery, session) -> None:
    await _admin_list(cb, session)
    await cb.answer()


async def _item_menu(cb: CallbackQuery, session, eid: int) -> None:
    repo = EventRepository(session)
    ev = await repo.get(eid)
    if ev is None:
        await cb.answer("Мероприятие удалено.", show_alert=True)
        await _admin_list(cb, session)
        return
    text = "🛠 <b>Управление мероприятием</b>\n\n" + _summary(ev)
    b = InlineKeyboardBuilder()
    for key, label in FIELDS:
        b.button(text=f"✏️ {label}", callback_data=f"evadmin:set:{eid}:{key}")
        _vrow(b)
    if ev.image_url:
        b.button(text="🖼 Убрать картинку", callback_data=f"evadmin:nopic:{eid}")
        _vrow(b)
    b.button(text="🗑 Удалить мероприятие", callback_data=f"evadmin:delq:{eid}")
    _vrow(b)
    b.button(text="⬅️ К списку", callback_data="evadmin:list")
    b.button(text="🏠 Меню", callback_data="menu:main")
    _vrow(b)
    await safe_edit_or_answer(cb.message, text, reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("evadmin:item:"))
async def evadmin_item(cb: CallbackQuery, session) -> None:
    if not _is_event_admin(cb.from_user.id):
        await cb.answer("Только для админов.", show_alert=True)
        return
    try:
        eid = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer()
        return
    await _item_menu(cb, session, eid)
    await cb.answer()


async def _ask(message: Message, state: FSMContext, key: str) -> None:
    data = await state.get_data()
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отменить", callback_data=data.get("ev_cancel_cb", "evadmin:home"))
    label = dict(FIELDS)[key]
    await message.answer(f"✏️ <b>{label}</b>\n{PROMPTS[key]}\n\n"
                         f"Отправь значение сообщением.",
                         reply_markup=b.as_markup())


@router.callback_query(F.data == "evadmin:add")
async def evadmin_add_start(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_event_admin(cb.from_user.id):
        await cb.answer("Только для админов.", show_alert=True)
        return
    await state.set_state(EvStates.awaiting)
    await state.update_data(mode="add", step=0, draft={},
                            ev_cancel_cb="evadmin:list")
    await cb.answer()
    await _ask(cb.message, state, "title")


@router.callback_query(F.data.startswith("evadmin:set:"))
async def evadmin_set_field(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_event_admin(cb.from_user.id):
        await cb.answer("Только для админов.", show_alert=True)
        return
    try:
        _, _, eid_s, key = cb.data.split(":")
        eid = int(eid_s)
    except (ValueError, IndexError):
        await cb.answer()
        return
    if key not in dict(FIELDS):
        await cb.answer()
        return
    await state.set_state(EvStates.awaiting)
    await state.update_data(mode="set", eid=eid, key=key,
                            ev_cancel_cb=f"evadmin:item:{eid}")
    await cb.answer()
    await _ask(cb.message, state, key)


@router.callback_query(F.data.startswith("evadmin:nopic:"))
async def evadmin_nopic(cb: CallbackQuery, session) -> None:
    if not _is_event_admin(cb.from_user.id):
        await cb.answer("Только для админов.", show_alert=True)
        return
    try:
        eid = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer()
        return
    repo = EventRepository(session)
    ev = await repo.get(eid)
    if ev is not None:
        ev.image_url = None
        await session.commit()
    await cb.answer("Картинка убрана.")
    await _item_menu(cb, session, eid)


@router.callback_query(F.data.startswith("evadmin:delq:"))
async def evadmin_del_ask(cb: CallbackQuery, session) -> None:
    if not _is_event_admin(cb.from_user.id):
        await cb.answer("Только для админов.", show_alert=True)
        return
    try:
        eid = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer()
        return
    repo = EventRepository(session)
    ev = await repo.get(eid)
    if ev is None:
        await cb.answer("Уже удалено.")
        return
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, удалить", callback_data=f"evadmin:del:{eid}")
    b.button(text="❌ Отмена", callback_data=f"evadmin:item:{eid}")
    _vrow(b)
    await safe_edit_or_answer(
        cb.message,
        f"⚠️ Удалить мероприятие <b>{html.escape(ev.title)}</b> безвозвратно?",
        reply_markup=b.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("evadmin:del:"))
async def evadmin_del(cb: CallbackQuery, session) -> None:
    if not _is_event_admin(cb.from_user.id):
        await cb.answer("Только для админов.", show_alert=True)
        return
    try:
        eid = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer()
        return
    repo = EventRepository(session)
    ok = await repo.delete(eid)
    await session.commit()
    await cb.answer("Удалено 🗑" if ok else "Не найдено.")
    await _admin_list(cb, session)


@router.message(EvStates.awaiting, CommandStart())
async def ev_cancel_by_start(message: Message, state: FSMContext) -> None:
    await state.clear()


@router.message(EvStates.awaiting, F.text == "/cancel")
async def ev_cancel_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Отменено. Жми «📅 Мероприятия» в меню.")


@router.message(EvStates.awaiting, F.chat.type == ChatType.PRIVATE, F.text)
async def ev_wizard_text(message: Message, state: FSMContext, session) -> None:
    data = await state.get_data()
    mode = data.get("mode")
    value = (message.text or "").strip()
    if not value:
        await message.answer("Пусто — отправь текст или /cancel.")
        return
    if mode == "set":
        key = data.get("key")
        eid = data.get("eid")
        repo = EventRepository(session)
        fields: dict = {}
        if key == "date":
            fields["date"] = _parse_date(value) or value
        elif key in ("image_url", "url"):
            if value == "-":
                fields[key] = None
            elif not value.lower().startswith(("http://", "https://", "tg://")):
                await message.answer("⚠️ Нужна ссылка http(s)://… (или «-», чтобы убрать).")
                return
            else:
                fields[key] = value
        elif value == "-" and key in ("time", "place", "meet", "description"):
            fields[key] = ""
        else:
            fields[key] = value
        if key == "title" and value != "-":
            pass
        await repo.update(eid, **fields)
        await session.commit()
        await state.clear()
        ev = await repo.get(eid)
        if ev is not None:
            await message.answer("✅ Обновлено!\n\n" + _summary(ev))
        return
    steps = ADD_STEPS
    i = int(data.get("step", 0))
    draft = dict(data.get("draft") or {})
    key = steps[i]
    if key == "date":
        parsed = _parse_date(value)
        if not parsed and value != "-":
            await message.answer("⚠️ Не понял дату. Формат: 1 октября, 01.10 или 2026-10-01.")
            return
        draft[key] = parsed or ""
    else:
        draft[key] = "" if value == "-" else value
    i += 1
    if i < len(steps):
        await state.update_data(step=i, draft=draft)
        await _ask(message, state, steps[i])
        return
    repo = EventRepository(session)
    ev = await repo.create(title=draft.get("title") or "Без названия",
                           date=draft.get("date", ""), time=draft.get("time", ""),
                           place=draft.get("place", ""), meet=draft.get("meet", ""),
                           description=draft.get("description", ""))
    await session.commit()
    await state.clear()
    b = InlineKeyboardBuilder()
    b.button(text="🖼 Добавить афишу", callback_data=f"evadmin:set:{ev.id}:image_url")
    _vrow(b)
    b.button(text="🔗 Добавить ссылку", callback_data=f"evadmin:set:{ev.id}:url")
    _vrow(b)
    b.button(text="🛠 Редактировать", callback_data=f"evadmin:item:{ev.id}")
    b.button(text="📋 К списку", callback_data="evadmin:list")
    _vrow(b)
    await message.answer("✅ Мероприятие создано!\n\n" + _summary(ev),
                         reply_markup=b.as_markup())
