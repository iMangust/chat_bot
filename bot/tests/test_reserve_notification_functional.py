"""Функциональный тест уведомления о новой броне мерча.

Требования из ТЗ:
 1) Уведомление «🛒 НОВАЯ БРОНЬ МЕРЧА» не должно содержать кнопок, которые
    могут случайно подтвердить продажу или снять резерв. Допустимы только
    url-кнопки (профиль покупателя / deep-link на карточку) — они ничего не
    меняют в базе.
 2) Дальнейшее взаимодействие открывается по ссылке: карточка брони с
    кнопками «💬 Написать покупателю», «✅ Подтвердить продажу»,
    «❌ Отменить резерв».
 3) Продажа/отмена требуют явного второго подтверждения
    (merch:<action>:yes:<vid>) — случайный клик по «sold»/«cancel» базу НЕ
    меняет.

Сеть до Telegram подменена фейковой сессией; БД — sqlite в памяти.
Запуск из каталога bot/:  pytest tests/test_reserve_notification_functional.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT_DIR)

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["BOT_TOKEN"] = "123456:TEST-token-for-functional-test"
os.environ["REDIS_URL"] = ""
os.environ["CHANNELS"] = "[]"
os.environ["ADMIN_IDS"] = "[42]"          # админ мерча = Test User (id 42)
os.environ["MERCH_ADMIN_ID"] = "42"
os.environ["BOT_USERNAME"] = "test_bot"   # для deep-link кнопок уведомлений

from app.config import get_settings as _gs

_gs.cache_clear()

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.session as _dbs

_s = _gs()
_dbs.engine = create_async_engine(_s.database_url)
_dbs.session_factory = async_sessionmaker(
    _dbs.engine, class_=_dbs.AsyncSession, expire_on_commit=False)

from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, SendChatAction
from aiogram.types import CallbackQuery, Chat, Message, Update, User


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.record: list[tuple[str, dict]] = []

    async def close(self) -> None:  # pragma: no cover
        pass

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__

        def _ser(v):
            if hasattr(v, "model_dump"):
                return v.model_dump(mode="json", by_alias=True, exclude_none=True)
            return v

        data = {}
        for k, v in dict(method).items():
            if v is None or k == "bot":
                continue
            data[k] = _ser(v)
        self.record.append((name, data))
        if isinstance(method, GetMe):
            return {"id": 123456, "is_bot": True,
                    "first_name": "test", "username": "test_bot"}
        if isinstance(method, SendChatAction):
            return True
        mid = len(self.record) + 1000
        raw = {
            "ok": True,
            "result": {
                "message_id": mid,
                "date": int(datetime.now().timestamp()),
                "chat": {"id": 42, "type": "private",
                         "first_name": "Test", "last_name": "User"},
                "from": {"id": 123456, "is_bot": True,
                         "first_name": "test", "username": "test_bot"},
                "text": data.get("text") or data.get("caption") or "",
            },
        }
        if name == "SendPhoto":
            raw["result"]["photo"] = [
                {"file_id": "f1", "file_unique_id": "u1",
                 "width": 10, "height": 10},
            ]
        return raw

    async def stream_content(self, url, headers=None, timeout=30,
                             chunk_size=4096, raise_for_status=True):
        yield b""


def _user(uid: int, first: str, uname: str | None) -> User:
    return User(id=uid, is_bot=False, first_name=first, username=uname)


def _build_cb_update(bot, data: str, uid: int = 42,
                     uname: str | None = "testuser") -> Update:
    chat = Chat(id=42, type="private", first_name="Test", last_name="User")
    msg = Message(message_id=1, date=datetime.now(), chat=chat).as_(bot)
    cb = CallbackQuery(
        id=str(uuid.uuid4()),
        from_user=_user(uid, "Test", uname),
        chat_instance=str(uuid.uuid4()),
        data=data,
        message=msg,
    )
    return Update(update_id=1, callback_query=cb)


async def _build_dp():
    """Диспетчер с той же регистрацией роутеров, что в main.py."""
    from aiogram import Dispatcher

    import app.db.session as dbs
    from app.config import get_settings
    from app.db.models import Base
    from app.handlers import access as access_handlers
    from app.handlers import (
        admin,
        arena,
        errors,
        events,
        games,
        merch,
        shop,
        social,
        start,
        stats,
        tamagotchi,
        tracker,
    )
    from app.handlers import settings as settings_h
    from app.main import _make_fsm_storage, probe_fsm_storage

    async with dbs.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    dp = Dispatcher(storage=await probe_fsm_storage(
        _make_fsm_storage(get_settings().redis_url)))
    dp.update.outer_middleware(dbs.DbMiddleware())
    dp.callback_query.outer_middleware(errors.ErrorNotifyMiddleware())

    modmap = dict(access_handlers=access_handlers, admin=admin, arena=arena,
                  errors=errors, events=events, games=games, merch=merch,
                  settings_h=settings_h, shop=shop, social=social,
                  start=start, stats=stats, tamagotchi=tamagotchi,
                  tracker=tracker)
    order = [
        "errors.error_router", "admin.router", "access_handlers.router",
        "start.router", "tracker.router", "tamagotchi.router", "games.router",
        "shop.router", "merch.router", "events.router", "social.router",
        "arena.router", "stats.router", "settings_h.router",
    ]
    routers = []
    for spec in order:
        mod, attr = spec.split(".")
        routers.append(getattr(modmap[mod], attr))
    for rt in routers:
        parent = getattr(rt, "_parent_router", None)
        if parent is not None and rt in parent.sub_routers:
            parent.sub_routers.remove(rt)
        rt._parent_router = None
        for child in list(rt.sub_routers):
            child._parent_router = None
        rt.sub_routers.clear()
    dp.include_routers(*routers)
    dp.errors.register(errors.on_error)
    return dp


async def _ensure_schema() -> None:
    from app.db.models import Base
    async with _dbs.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_variant() -> int:
    """Категория + товар + позиция с остатком. Возвращает variant_id."""
    from app.db.repositories import MerchRepository
    await _ensure_schema()
    async with _dbs.session_factory() as s:
        repo = MerchRepository(s)
        cat = await repo.add_category("caps", "Кепки")
        prod = await repo.add_product(cat.id, "НЕ ПИЗДЕТЬ, А ДЕЛАТЬ")
        v, _new = await repo.add_variant(prod.id, "M", "Розовый", 2499, 3)
        vid = v.id
        await s.commit()
    return vid


def _kb_buttons(d: dict) -> list[dict]:
    """Развернуть reply_markup метода в плоский список inline-кнопок."""
    kb = d.get("reply_markup")
    if kb is None:
        return []
    if hasattr(kb, "inline_keyboard"):
        rows = kb.inline_keyboard
    else:
        if isinstance(kb, str):
            kb = json.loads(kb)
        rows = [[_mk(b) for b in row] for row in kb.get("inline_keyboard", [])]
    out = []
    for row in rows:
        for btn in row:
            out.append(btn if isinstance(btn, dict) else btn.model_dump(
                mode="json", exclude_none=True))
    return out


def _mk(b):  # InlineKeyboardButton → dict-friendly
    return b.model_dump(mode="json", exclude_none=True) if hasattr(b, "model_dump") else b


async def _reserve_and_capture(buyer_uid: int = 777) -> tuple[FakeSession, int]:
    """Пользователь-покупатель бронит позицию; возвращаем запись его запросов.

    Возвращает (сессия_уведомлений, variant_id) — сессию нужно держать живой
    (она используется и дальше в тестах двухшагового подтверждения).
    """
    session, vid, _dp, _bot = await _reserve_full()
    return session, vid


async def _reserve_full(buyer_uid: int = 777):
    """Как _reserve_and_capture, но дополнительно возвращает dp и бота."""
    vid = await _seed_variant()
    dp = await _build_dp()
    session = FakeSession()
    from aiogram import Bot

    from app.config import get_settings
    bot = Bot(token=get_settings().bot_token, session=session)
    await dp.feed_update(bot, _build_cb_update(
        bot, f"merch:res:{vid}", uid=buyer_uid, uname="jMangust"))
    return session, vid, dp, bot


def test_notification_has_no_confirm_buttons():
    """Уведомление админу: есть, но без callback-кнопок sold/cancel/myres."""
    async def run():
        session, _vid = await _reserve_and_capture()
        notes = [d for m, d in session.record
                 if m == "SendMessage" and "НОВАЯ БРОНЬ" in str(d.get("text", ""))]
        assert notes, f"уведомление админу не отправлено: {[m for m, _ in session.record]}"
        text = notes[0]["text"]
        assert "Mangust" in text or "jMangust" in text, f"нет данных покупателя: {text}"
        assert "2,499 ₽" in text or "2 499 ₽" in text or "2499 ₽" in text, \
            f"нет цены: {text}"
        buttons = _kb_buttons(notes[0])
        cbs = [b.get("callback_data") for b in buttons if b.get("callback_data")]
        forbidden = ("merch:sold", "merch:cancel", "merch:myres", "merch:resitem")
        bad = [c for c in cbs if any(c.startswith(f) for f in forbidden)]
        assert not bad, f"в уведомлении есть опасные callback-кнопки: {bad}"
        # допустимы только url-кнопки (ничего в БД не меняют)
        for b in buttons:
            assert "url" in b, f"не-url кнопка в уведомлении: {b}"
    asyncio.run(run())


def test_deep_link_opens_reserve_card_with_actions():
    """Deep-link «🧾 Открыть бронь» → карточка с действиями и ссылкой на покупателя."""
    async def run():
        session, vid = await _reserve_and_capture()
        notes = [d for m, d in session.record
                 if m == "SendMessage" and "НОВАЯ БРОНЬ" in str(d.get("text", ""))]
        buttons = _kb_buttons(notes[0])
        card = next((b for b in buttons
                     if "Открыть бронь" in b.get("text", "")), None)
        assert card, f"нет кнопки «Открыть бронь»: {buttons}"
        url = card["url"]
        assert f"?start=nav_rescard_{vid}" in url, f"битый deep-link: {url}"

        # «нажатие» на ссылку = /start nav_rescard_<vid> от имени админа (42).
        # Идём напрямую в merch_reserve_card_open — тот же путь, что у
        # cmd_start → _handle_nav_payload (его покрытие — ниже, smoke-тестом).
        s2 = FakeSession()
        from aiogram import Bot

        from app.config import get_settings
        bot = Bot(token=get_settings().bot_token, session=s2)
        chat = Chat(id=42, type="private", first_name="Test", last_name="User")
        msg = Message(message_id=5, date=datetime.now(), chat=chat,
                      from_user=_user(42, "Test", "testuser"),
                      text=f"/start nav_rescard_{vid}").as_(bot)
        from datetime import datetime as _dt

        from app.db.models import User
        from app.handlers.merch import merch_reserve_card_open
        async with _dbs.session_factory() as s_:
            if not await s_.get(User, 777):
                s_.add(User(tg_id=777, first_name="Mangust", username="jMangust",
                            lang="ru", coins=0, xp=0, level=1, streak_days=0,
                            best_streak=0, onboarded=True, is_banned=False,
                            settings_extra={}, welcome_shown=True,
                            messages_count=0, reactions_given=0,
                            reactions_received=0,
                            created_at=_dt.now(), updated_at=_dt.now()))
                await s_.commit()
            await merch_reserve_card_open(msg, s_, vid, 42)
        caps = [str(d.get("text", "")) for m, d in s2.record if m == "SendMessage"]
        body = "\n".join(caps)
        assert f"Бронь #{vid}" in body, f"карточка брони не открыта: {caps}"
        last = [d for m, d in s2.record
                if m == "SendMessage" and f"Бронь #{vid}" in str(d.get("text", ""))][-1]
        buttons = _kb_buttons(last)
        texts = " | ".join(b.get("text", "") for b in buttons)
        assert "Написать покупателю" in texts, f"нет ссылки на покупателя: {texts}"
        assert any(b.get("url", "").endswith("jMangust") for b in buttons), \
            f"кнопка покупателя не ведёт в профиль: {buttons}"
        assert "Подтвердить продажу" in texts and "Отменить резерв" in texts, texts
    asyncio.run(run())


def test_sold_requires_confirmation():
    """Клик «✅ Подтвердить продажу» без yes — БД не тронута, ждём подтверждения."""
    async def run():
        session, vid, dp, bot = await _reserve_full()
        from app.db.repositories import MerchRepository
        # Шаг 1: клик по «Подтвердить продажу» (из карточки брони)
        await dp.feed_update(bot, _build_cb_update(bot, f"merch:sold:{vid}"))
        async with _dbs.session_factory() as s_:
            v = await MerchRepository(s_).get_variant(vid)
            assert v.reserved_by == 777, "продажа применилась без подтверждения!"
            assert v.stock == 3, f"остаток списан без подтверждения: {v.stock}"
        all_text = "\n".join(str(d.get("text", "")) + str(d.get("caption", ""))
                             for m, d in session.record[-4:]
                             if m in ("SendMessage", "EditMessageText"))
        assert "Подтверди" in all_text or "подтвержд" in all_text.lower(), \
            f"экран подтверждения не показан: {all_text[:300]}"

        # Шаг 2: «Да, подтвердить» — теперь продажа проходит
        await dp.feed_update(bot, _build_cb_update(bot, f"merch:sold:yes:{vid}"))
        async with _dbs.session_factory() as s_:
            v = await MerchRepository(s_).get_variant(vid)
            assert v.reserved_by is None, "подтверждённая продажа не сняла бронь"
            assert v.stock == 2, f"остаток не списан: {v.stock}"
    asyncio.run(run())


def test_cancel_requires_confirmation():
    """Клик «❌ Отменить резерв» без yes — бронь на месте; yes — снимается."""
    async def run():
        session, vid, dp, bot = await _reserve_full()
        from app.db.repositories import MerchRepository
        await dp.feed_update(bot, _build_cb_update(bot, f"merch:cancel:{vid}"))
        async with _dbs.session_factory() as s_:
            v = await MerchRepository(s_).get_variant(vid)
            assert v.reserved_by == 777, "резерв снят без подтверждения!"
        await dp.feed_update(bot, _build_cb_update(bot, f"merch:cancel:yes:{vid}"))
        async with _dbs.session_factory() as s_:
            v = await MerchRepository(s_).get_variant(vid)
            assert v.reserved_by is None, "подтверждённая отмена не сняла бронь"
    asyncio.run(run())


def test_myres_lists_reserves_without_direct_actions():
    """Экран «Все брони» — список карточек, а не кнопки sold/cancel сразу."""
    async def run():
        _session, vid = await _reserve_and_capture()
        dp = await _build_dp()
        from aiogram import Bot

        from app.config import get_settings
        s2 = FakeSession()
        bot = Bot(token=get_settings().bot_token, session=s2)
        await dp.feed_update(bot, _build_cb_update(bot, "merch:myres"))
        sends = [(d, _kb_buttons(d)) for m, d in s2.record if m == "SendMessage"]
        target = [(d, btns) for d, btns in sends
                  if "Активные брони" in str(d.get("text", ""))]
        assert target, f"список броней не показан: {[str(d.get('text',''))[:80] for d,_ in sends]}"
        d, btns = target[0]
        cbs = [b.get("callback_data", "") for b in btns]
        assert any(c == f"merch:resitem:{vid}" for c in cbs), \
            f"нет кнопки-карточки брони: {cbs}"
        assert not any(c.startswith(("merch:sold:", "merch:cancel:")) for c in cbs), \
            f"на списке висят прямые кнопки продажи/отмены: {cbs}"
    asyncio.run(run())


def test_main():
    test_notification_has_no_confirm_buttons()
    test_deep_link_opens_reserve_card_with_actions()
    test_sold_requires_confirmation()
    test_cancel_requires_confirmation()
    test_myres_lists_reserves_without_direct_actions()
    print("RESERVE NOTIFICATION FUNCTIONAL TEST OK")


if __name__ == "__main__":
    test_main()
