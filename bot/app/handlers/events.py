from __future__ import annotations

import contextlib
import html
import re

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import BaseFilter, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InputMediaPhoto, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

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


async def _render_list(cb: CallbackQuery, session, bot: Bot | None = None) -> None:
    # Дебаг-логирование входа и результата запроса. Если хендлер не вызывается,
    # в логах не будет строк «menu:events handler» — это сразу отделяет проблему
    # диспетчера (роутинг/фильтры) от проблем рендера (БД/Telegram API).
    logger.info("menu:events handler entered (user={}, data={!r})",
                cb.from_user.id if cb.from_user else "?", cb.data)
    repo = EventRepository(session)
    try:
        all_ev = await repo.all()
    except Exception as exc:
        from sqlalchemy.exc import DisconnectionError, OperationalError, ProgrammingError
        if not isinstance(exc, (OperationalError, ProgrammingError,
                                DisconnectionError)):
            logger.exception("menu:events DB query failed: {}", exc)
            raise
        # САМОЛЕЧЕНИЕ «мертвой» кнопки: самая частая причина OperationalError
        # здесь — таблица events отсутствует в старой БД (её никогда не
        # создавали миграции). Создаём её по модели и повторяем запрос.
        logger.warning("menu:events DB error {!r}: {} — trying self-heal "
                       "(create 'events' table)", type(exc).__name__, exc)
        all_ev = await _selfheal_reload_events(exc, session)
    logger.info("menu:events loaded {} events from DB", len(all_ev))
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
    # ВАЖНО: InlineKeyboardBuilder.as_markup() НЕ сбрасывает накопленные кнопки.
    # Если вызвать его дважды (например, при ретрае после «message is not
    # modified»), во второй разметке каждая кнопка продублируется и Telegram
    # отклонит edit с ошибкой «BUTTON_DATA_INVALID» — пользователь увидит
    # «ничего не происходит». Фиксируем разметку ровно один раз.
    markup = b.as_markup()
    # Дефолтный Message в aiogram 3.x не «смонтирован» на конкретный Bot,
    # поэтому target.edit_text()/answer() без явного монтирования падает с
    # RuntimeError («This method is not mounted to a any bot instance») — и
    # пользователь видит «ничего не происходит». Монтируем методы явно на bot
    # из контекста хендлера. Дополнительно: если у исходного сообщения нет
    # текста, Telegram отклоняет EditMessageText — шлём новое сообщение.
    async def _render_on(msg: Message) -> None:
        from aiogram.methods import EditMessageText, SendMessage
        if msg.text or msg.caption:
            await bot(EditMessageText(
                chat_id=msg.chat.id, message_id=msg.message_id,
                text=text, parse_mode=ParseMode.HTML, reply_markup=markup))
        else:
            logger.warning("menu:events source message has no text; sending new instead")
            await bot(SendMessage(
                chat_id=msg.chat.id, text=text,
                parse_mode=ParseMode.HTML, reply_markup=markup))

    if cb.message is not None and bot is not None:
        try:
            await _render_on(cb.message)
        except Exception as exc:
            # «message is not modified» — штатная ситуация (пользователь
            # повторно нажал кнопку и экран уже актуален). Не считаем это
            # ошибкой: просто отвечаем на callback без редизплея.
            low = str(exc).lower()
            if "not modified" in low:
                logger.debug("menu:events edit skipped (not modified)")
                return
            raise
        return
    # cb.message отсутствует (например, инлайн-сообщение в каталоге) или bot
    # не передан (вызов из админ-хелперов) — используем старый путь; иначе
    # safe_edit_or_answer(None, ...) падал с AttributeError.
    if cb.message is not None:
        await safe_edit_or_answer(cb.message, text, reply_markup=markup)
    elif bot is not None:
        from aiogram.methods import SendMessage
        await bot(SendMessage(chat_id=cb.from_user.id, text=text,
                              parse_mode=ParseMode.HTML, reply_markup=markup))


async def _selfheal_reload_events(exc: Exception, session) -> list:
    """Восстановление после OperationalError на запросе мероприятий.

    1) Если в БД нет таблицы events — создаём её по модели (миграция v2.0.3).
    2) Откатываем сессию обработчика (после ошибки транзакция «aborted» на PG).
    3) Читаем список событий новой отдельной сессией, чтобы не зависеть от
       состояния сессии запросившего апдейта.
    """
    import app.db.session as dbs
    from app.db.models import Event
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import select

    # 1) Досоздать отсутствующую таблицу events (по модели, идемпотентно).
    try:
        from app.main import ensure_events_table
        created = await ensure_events_table(dbs.engine)
        if created:
            logger.warning("menu:events self-heal: таблица 'events' создана")
    except Exception as heal_exc:
        logger.warning("menu:events self-heal create failed: {!r}: {}",
                       type(heal_exc).__name__, heal_exc)

    # 2) Откатить сессию апдейта (на PostgreSQL после ошибки транзакции все
    #    следующие запросы в ней падают с InFailedSqlTransaction/OperationalError).
    with contextlib.suppress(Exception):
        await session.rollback()

    # 3) Перечитать новой сессией; при этом проверяем наличие таблицы напрямую,
    #    чтобы дать понятный лог вместо повторного «no such table».
    try:
        async with dbs.engine.connect() as conn:
            has_tbl = await conn.run_sync(
                lambda sc: sa_inspect(sc).has_table("events"))
        if not has_tbl:
            logger.error("menu:events self-heal: таблица 'events' так и не "
                         "создана — проверьте права на БД и пришлите лог запуска")
            return []
        async with dbs.session_factory() as s2:
            return list((await s2.execute(
                select(Event).order_by(Event.date, Event.id)
            )).scalars().all())
    except Exception as exc2:
        logger.exception("menu:events self-heal reload failed: {} ({})",
                         exc2, type(exc2).__name__)
        raise


class _MenuFallback(BaseFilter):
    """Catch-all для кнопок «menu:*», которые больше никем не обработаны.

    Регистрируется ПОСЛЕДЕЙ в роутере, поэтому срабатывает только если ни один
    конкретный обработчик не подошёл (например, устаревшая клавиатура из старой
    версии бота). Специально реализован как фильтр по callback_data, а не через
    CommandStart: CommandStart матчит только текст сообщения, начинающийся с
    «/menu…», и никогда не сработает на callback-кнопках вида «menu:events».
    """

    async def __call__(self, cb: CallbackQuery) -> bool:
        return bool(cb.data) and cb.data.startswith("menu:")


@router.callback_query(F.data == "menu:events")
async def menu_events(cb: CallbackQuery, session, bot: Bot) -> None:
    from aiogram.methods import AnswerCallbackQuery
    # Если Telegram не прислал исходное сообщение (cb.message is None —
    # бывает при очень старых/анонимных клавиатурах и у некоторых клиентов),
    # раньше бот молча «ничего не делал»: показываем явный alert.
    if cb.message is None:
        with contextlib.suppress(Exception):
            await bot(AnswerCallbackQuery(
                callback_query_id=cb.id,
                text="Нажми /start — покажу свежее меню 🙂", show_alert=True))
        logger.warning("menu:events without message (user={})",
                       cb.from_user.id if cb.from_user else "?")
        return
    try:
        await _render_list(cb, session, bot=bot)
    except Exception as exc:
        logger.exception("menu:events render failed: {}", exc)
        # Явная обратная связь вместо «тихого» ничего-не-происходит: если
        # сообщение отредактировать не удалось (или упал БД/Telegram),
        # показываем alert — пользователь всегда видит реакцию на нажатие.
        with contextlib.suppress(Exception):
            await bot(AnswerCallbackQuery(
                callback_query_id=cb.id,
                text="Не удалось загрузить мероприятия 😅", show_alert=True))
        return
    with contextlib.suppress(Exception):
        await bot(AnswerCallbackQuery(callback_query_id=cb.id))


# ВАЖНО: этот обработчик должен оставаться последним в файле/роутере —
# он ловит только непойманные «menu:*» коллбэки (aiogram вызывает handlers
# в порядке регистрации).
@router.callback_query(_MenuFallback())
async def menu_any_unhandled(cb: CallbackQuery, bot: Bot) -> None:
    # НИЧЕГО не удаляем из исходного сообщения (раньше здесь вызывался
    # edit_reply_markup(None), который стирал ВСЮ клавиатуру главного меню —
    # после одного «устаревшего» нажатия все кнопки исчезали). Только отвечаем
    # на callback и подсказываем пользователю.
    await cb.answer("Кнопка устарела 😅 Напиши /start — покажу свежее меню.",
                    show_alert=False)
    logger.warning("unhandled menu callback: {!r} (user={})", cb.data,
                   cb.from_user.id if cb.from_user else "?")


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
    # Явная реакция на каждое нажатие (раньше при отсутствии прав или битом
    # сообщении callback не answer'ился вовсе — кнопка выглядела «мёртвой»).
    if cb.message is None:
        with contextlib.suppress(Exception):
            await cb.answer("Нажми /start — покажу свежее меню 🙂", show_alert=True)
        return
    if not _is_event_admin(cb.from_user.id):
        s = get_settings()
        logger.warning("evadmin:home DENIED user={} (admin_ids={!r}, "
                       "merch_admin_id={!r})", cb.from_user.id,
                       s.admin_ids, s.merch_admin_id)
        await cb.answer("Только для админов. Добавь свой ID в ADMIN_IDS в .env "
                        "и перезапусти бота.", show_alert=True)
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
    if cb.message is None:
        with contextlib.suppress(Exception):
            await cb.answer("Нажми /start — покажу свежее меню 🙂", show_alert=True)
        return
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
