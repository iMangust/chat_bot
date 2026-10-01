"""Функциональный тест мастера мероприятий: опциональные шаги + афиша-фото.

Проверяет новые требования:
1) Шаг «Сбор» (meet) необязателен — пропускается кнопкой evadmin:skip:meet;
2) «Афиша» больше не запрашивается ссылкой: картинка загружается сообщением
   (как фото товара в мерче) и хранится file_id в Event.image_url;
3) В режиме редактирования пропуск необязательного поля очищает его.

Реально гоняем апдейты через Dispatcher с фейковой HTTP-сессией Bot API
и sqlite в памяти — как в test_events_menu_functional.py.

Запуск из каталога bot/:  pytest tests/test_events_wizard_functional.py -v
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
# Админ = id 42, чтобы проходить проверки _is_event_admin.
os.environ["ADMIN_IDS"] = "[42]"

from app.config import get_settings as _gs  # noqa: E402
_gs.cache_clear()

import app.db.session as _dbs  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

_s = _gs()
_dbs.engine = create_async_engine(_s.database_url)
_dbs.session_factory = async_sessionmaker(
    _dbs.engine, class_=_dbs.AsyncSession, expire_on_commit=False)

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.methods import GetMe, SendChatAction  # noqa: E402
from aiogram.types import (CallbackQuery, Chat, Message, PhotoSize, Update,  # noqa: E402
                           User)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.record: list[tuple[str, dict]] = []

    async def close(self) -> None:
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
        resp = {
            "message_id": mid,
            "date": int(datetime.now().timestamp()),
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 123456, "is_bot": True,
                     "first_name": "test", "username": "test_bot"},
            "text": data.get("text") or data.get("caption") or "",
        }
        if name == "SendPhoto":
            resp["photo"] = [{"file_id": "SAVED_FILE_ID_XYZ",
                              "file_unique_id": "u1", "width": 100, "height": 100}]
        return resp

    async def stream_content(self, url, headers=None, timeout=30,
                             chunk_size=4096, raise_for_status=True):
        yield b""


CHAT = Chat(id=42, type="private")
USER = User(id=42, is_bot=False, first_name="Test")


def _msg(bot: Bot, *, text=None, photo=None) -> Message:
    m = Message(message_id=int(uuid.uuid4().int % 10**9), date=datetime.now(),
                chat=CHAT, from_user=USER, text=text, photo=photo)
    return m.as_(bot)


def _cb(bot: Bot, data: str) -> CallbackQuery:
    # from_user у сообщения = тот же пользователь, что нажимает (для is_sender).
    m = Message(message_id=int(uuid.uuid4().int % 10**9), date=datetime.now(),
                chat=CHAT, from_user=USER)
    return CallbackQuery(id=str(uuid.uuid4()), from_user=USER,
                         chat_instance=str(uuid.uuid4()), data=data,
                         message=m.as_(bot)).as_(bot)


def _msg_texts(session: FakeSession) -> list[tuple[int, str]]:
    """Тексты сообщений вместе с индексом записи (устойчив к служебным вызовам)."""
    out: list[tuple[int, str]] = []
    for i, (name, d) in enumerate(session.record):
        if name in ("SendMessage", "EditMessageText"):
            out.append((i, str(d.get("text", ""))))
        elif name in ("SendPhoto", "EditMessageMedia", "EditMessageCaption"):
            media = d.get("media") or {}
            out.append((i, str(media.get("caption", "") if isinstance(media, dict) else "")))
    return out


def _texts(session: FakeSession) -> list[str]:
    return [t for _, t in _msg_texts(session)]


def _kbs(session: FakeSession) -> list[str]:
    return [json.dumps(d.get("reply_markup", {}), ensure_ascii=False)
            for _, d in session.record]


async def _build_dp() -> tuple[Dispatcher, Bot, FakeSession]:
    import app.db.session as dbs
    from app.config import get_settings
    from app.db.models import Base
    from app.handlers import (access as access_handlers, admin, arena, errors,
                              events, games, merch, settings as settings_h,
                              shop, social, start, stats, tamagotchi, tracker)
    from app.main import _make_fsm_storage, probe_fsm_storage

    engine = dbs.engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    dp = Dispatcher(storage=await probe_fsm_storage(
        _make_fsm_storage(get_settings().redis_url)))
    dp.update.outer_middleware(dbs.DbMiddleware())
    dp.callback_query.outer_middleware(errors.ErrorNotifyMiddleware())
    # Роутеры модулей — синглтоны и могут быть «прикреплены» к другому
    # Dispatcher (например, если сначала прогонялся другой функциональный
    # тест). Отвязываем их перед повторной регистрацией.
    routers = [errors.error_router, admin.router, access_handlers.router,
               start.router, tracker.router, tamagotchi.router, games.router,
               shop.router, merch.router, events.router, social.router,
               arena.router, stats.router, settings_h.router]
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
    session = FakeSession()
    bot = Bot(token=get_settings().bot_token, session=session)
    return dp, bot, session


async def _run() -> None:
    dp, bot, session = await _build_dp()

    # ── Мастер создания: title → date → time(skip) → place(-) → meet(skip) → desc(-)
    await dp.feed_update(bot, Update(update_id=1, callback_query=_cb(bot, "evadmin:add")))
    await dp.feed_update(bot, Update(update_id=2, message=_msg(bot, text="Кани Уэст")))
    await dp.feed_update(bot, Update(update_id=3, message=_msg(bot, text="01.10")))

    # Шаг «Время»: должна быть кнопка ⏭ Пропустить
    assert any("evadmin:skip:time" in kb for kb in _kbs(session)), \
        "На шаге «Время» нет кнопки пропуска"
    n_before = len(session.record)
    await dp.feed_update(bot, Update(update_id=4, callback_query=_cb(bot, "evadmin:skip:time")))
    assert len(session.record) > n_before, "Пропуск шага ничего не ответил"

    # Шаг «Место»: пропускаем текстом «-»
    await dp.feed_update(bot, Update(update_id=5, message=_msg(bot, text="-")))

    # Шаг «Сбор»: тоже необязательный — кнопка + пропуск
    assert any("evadmin:skip:meet" in kb for kb in _kbs(session)), \
        "На шаге «Сбор» нет кнопки пропуска (шаг должен быть опциональным)"
    await dp.feed_update(bot, Update(update_id=6, callback_query=_cb(bot, "evadmin:skip:meet")))

    # Шаг «Описание»: «-»
    await dp.feed_update(bot, Update(update_id=7, message=_msg(bot, text="-")))

    texts = _texts(session)
    created = [t for t in texts if "Мероприятие создано" in t]
    assert created, f"Экран создания не показан; texts={texts}"
    # Сбор пустой — строки «Сбор:» быть не должно
    assert "Сбор:" not in created[-1]

    from app.db.models import Event
    async with _dbs.session_factory() as s:
        ev = (await s.execute(__import__("sqlalchemy").select(Event))).scalars().all()
        assert len(ev) == 1, f"Событие не создано: {ev}"
        ev0 = ev[0]
        assert ev0.title == "Кани Уэст" and ev0.date == "2026-10-01"
        assert ev0.time == "" and ev0.place == "" and ev0.meet == ""
        eid = ev0.id

    # ── Афиша: просим фото, а не ссылку
    idx_before = len(session.record)
    await dp.feed_update(bot, Update(
        update_id=8, callback_query=_cb(bot, f"evadmin:set:{eid}:image_url")))
    ask = [t for i, t in _msg_texts(session) if i >= idx_before]
    assert any("картинку" in t.lower() for t in ask), \
        f"Шаг афиши не просит картинку: {ask}"
    assert not any("Нужна ссылка" in t for t in ask)

    # Текстовое значение без URL — подсказка загрузить картинку
    await dp.feed_update(bot, Update(update_id=9, message=_msg(bot, text="просто текст")))
    assert any("картинку" in t.lower() for t in _texts(session)[-1:])

    # Присылаем фото — сохраняется file_id
    photo = [PhotoSize(file_id="in_file_id", file_unique_id="u",
                       width=100, height=100)]
    await dp.feed_update(bot, Update(update_id=10, message=_msg(bot, photo=photo)))
    assert any("Афиша сохранена" in t for t in _texts(session)), \
        f"Нет подтверждения сохранения афиши: {_texts(session)[-3:]}"
    async with _dbs.session_factory() as s:
        fresh = (await s.execute(
            __import__("sqlalchemy").select(Event).where(Event.id == eid))).scalar_one()
        assert fresh.image_url == "SAVED_FILE_ID_XYZ", \
            f"file_id не сохранён: {fresh.image_url!r}"

    # ── Редактирование: пропуск необязательного поля очищает его
    await dp.feed_update(bot, Update(
        update_id=11, callback_query=_cb(bot, f"evadmin:set:{eid}:meet")))
    await dp.feed_update(bot, Update(
        update_id=12, callback_query=_cb(bot, "evadmin:skip:meet")))
    async with _dbs.session_factory() as s:
        fresh = (await s.execute(
            __import__("sqlalchemy").select(Event).where(Event.id == eid))).scalar_one()
        assert fresh.meet == "", f"meet не очищен пропуском: {fresh.meet!r}"

    print("FUNCTIONAL TEST OK: optional steps skipped, poster saved as photo file_id")


def test_events_wizard_optional_steps_and_photo_poster():
    asyncio.run(_run())
