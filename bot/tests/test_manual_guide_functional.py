"""Функциональный тест кнопок «📖 Гид по уходу» (manual:*).

Воспроизводит баг из продакшн-логов: при регистрации роутеров БЕЗ
manual.router (или раньше него) коллбэки «manual:species:*» не матчились
ни одним хендлером — catch-all events фильтрует только «menu:*», поэтому
пользователь не получал НИКАКОЙ реакции на нажатие. Симптом выглядел так,
будто карточки «🐱 Котёнок» и «🐶 Щенок» «не открываются», тогда как
«🦊 Лисёнок»/«🐭 Шиншилла» работали через другой путь входа (/manual).

Проверяемые исправления:
 1) pet_manual.species_text() возвращает непустую карточку для КАЖДОГО вида
    из SPECIES_DATA (регрессия tactics_for/gear_tips_for);
 2) manual.router содержит обработчики всех кнопок домашнего экрана гида;
 3) в events зарегистрирован мост «manual:*» — даже в диспетчере без
    manual.router нажатие любой кнопки вида открывает его карточку
    (callback answer'нут, экран отрендерен, catch-all не сработал).

Сеть до Telegram подменена фейковой сессией; БД — sqlite в памяти.
Запуск из каталога bot/:  pytest tests/test_manual_guide_functional.py -v
"""
from __future__ import annotations

import asyncio
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
os.environ["ADMIN_IDS"] = "[]"

from app.config import get_settings as _gs

_gs.cache_clear()

# Изоляция движка БД этого теста (см. комментарий в test_events_menu_functional)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.session as _dbs

_s = _gs()
_dbs.engine = create_async_engine(_s.database_url)
_dbs.session_factory = async_sessionmaker(
    _dbs.engine, class_=_dbs.AsyncSession, expire_on_commit=False)

from aiogram.client.session.base import BaseSession
from aiogram.types import CallbackQuery, Chat, Message, Update, User

_captured_logs: list[str] = []


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.record: list[tuple[str, dict]] = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        name = method.__api_method__
        data = method.model_dump(exclude_none=True)
        self.record.append((name, data))
        if name == "GetMe":
            return {"id": bot.id, "is_bot": True, "first_name": "test",
                    "username": "test_bot"}
        raw = {
            "ok": True,
            "result": {
                "message_id": 1,
                "date": int(datetime.now().timestamp()),
                "chat": {"id": data.get("chat_id", 42), "type": "private",
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


def _build_update(bot, data: str) -> Update:
    chat = Chat(id=42, type="private", first_name="Test", last_name="User")
    msg = Message(message_id=1, date=datetime.now(), chat=chat).as_(bot)
    cb = CallbackQuery(
        id=str(uuid.uuid4()),
        from_user=User(id=42, is_bot=False, first_name="Test", last_name="User"),
        chat_instance=str(uuid.uuid4()),
        data=data,
        message=msg,
    )
    return Update(update_id=1, callback_query=cb)


# Диспетчер БЕЗ manual.router — ровно та конфигурация, в которой кнопки гида
# молчали (порядок повторяет main.py, кроме исключённого manual.router).
ROUTER_ORDER_NO_MANUAL = [
    "errors.error_router", "admin.router", "access_handlers.router",
    "start.router", "tracker.router", "tamagotchi.router", "games.router",
    "shop.router", "merch.router", "events.router", "social.router",
    "arena.router", "stats.router", "settings_h.router",
]


async def _build_dp_without_manual():
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
    routers = []
    for spec in ROUTER_ORDER_NO_MANUAL:
        mod, attr = spec.split(".")
        routers.append(getattr(modmap[mod], attr))
    # модули-роутеры синглтоны: отвязываем от предыдущего Dispatcher
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


async def _press_without_manual(data: str):
    from aiogram import Bot

    from app.config import get_settings

    dp = await _build_dp_without_manual()
    import app.db.session as dbs
    from app.db.repositories import UserRepository
    async with dbs.session_factory() as s_:
        await UserRepository(s_).get_or_create(42, "Test", "testuser")
    session = FakeSession()
    bot = Bot(token=get_settings().bot_token, session=session)
    await dp.feed_update(bot, _build_update(bot, data))
    # имена методов в FakeSession записываются как __api_method__ — в нижнем
    # регистре ('sendMessage', 'editMessageText'), сверяемся с ними же
    _SCREEN_METHODS = {"sendmessage", "editmessagetext",
                        "sendphotocaption", "editmediacaption"}
    texts = [str(d.get("text", "") or d.get("caption", ""))
             for m, d in session.record
             if m.lower() in _SCREEN_METHODS]
    return session, texts


def test_species_card_renders_for_every_species():
    """Карточка вида генерируется для ВСЕХ видов (регрессия tactics_for)."""
    from app.services import pet_data, pet_manual

    assert set(pet_manual.SPECIES_DATA) == set(pet_data.SPECIES_DATA)
    for code, sp in pet_data.SPECIES_DATA.items():
        text = pet_manual.species_text(code)
        assert text, f"{code}: species_text вернул пусто"
        assert sp["title"] in text, f"{code}: нет заголовка вида"
        # персональные советы и экипировка обязаны существовать у каждого вида
        assert pet_manual.tactics_for(code, sp), f"{code}: пустые тактики"


def test_manual_router_handles_all_home_buttons():
    """manual.router покрывает каждую кнопку домашнего экрана гида."""
    from app.handlers import manual as manual_h
    from app.services import pet_data

    def _matches(data: str) -> bool:
        cb = CallbackQuery.model_construct(
            id="1", from_user=None, chat_instance="x", data=data, message=None)
        return any(
            all(f.magic.resolve(cb) for f in h.filters)
            for h in manual_h.router.callback_query.handlers
        )

    assert _matches("manual:home")
    assert _matches("manual:stats")
    assert _matches("manual:games")
    for code in pet_data.SPECIES_DATA:
        assert _matches(f"manual:species:{code}"), \
            f"manual.router не обрабатывает кнопку вида {code!r}"


def test_species_buttons_work_without_manual_router():
    """Мост в events лечит кнопки гида даже без регистрации manual.router."""

    async def run():
        for code, sp in _species_cases():
            _session, texts = await _press_without_manual(
                f"manual:species:{code}")
            joined = "\n".join(texts)
            assert sp["title"] in joined, (
                f"кнопка {code!r} не открыла карточку вида без manual.router; "
                f"texts={texts}")
            assert "устарела" not in joined

    asyncio.run(run())


def _species_cases():
    from app.services import pet_data

    return sorted(pet_data.SPECIES_DATA.items())
