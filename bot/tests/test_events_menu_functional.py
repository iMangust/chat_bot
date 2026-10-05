"""Функциональный тест меню «Мероприятия».

Реально вызывает обработчик через Диспетчер (dp.feed_update) — то есть
проверяется вся цепочка: роутинг callback'а `menu:events`, middleware-и,
хендлер и рендер. Сеть до Telegram подменяем фейковым AbstractSession,
поэтому тест не требует реального токена/сервера БД (sqlite в памяти).

Запуск из каталога bot/:  pytest tests/test_events_menu_functional.py -v
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
# Локальный тест: пользователь id=42 НЕ админ — проверяем «пользовательскую»
# ветку меню (пустой список → заглушка). Явно обнуляем ADMIN_IDS, чтобы
# значение из .env репозитория или другого теста не превратило 42 в админа.
os.environ["ADMIN_IDS"] = "[]"

# Тесты запускаются в одном pytest-процессе; get_settings() — lru_cache.
# Сбрасываем кеш, чтобы настройки этого файла (в т.ч. ADMIN_IDS=[]) не
# наследовались от других тестов и наоборот.
from app.config import get_settings as _gs

_gs.cache_clear()

# ВАЖНО: app.db.session создаёт engine при импорте — если модуль уже был
# импортирован другим тестом процесса (с его файловой sqlite), движок
# указывал бы на чужую базу и видел её данные (регрессия: «Встреча» из
# теста admin_api попадала в экран мероприятий). Пересоздаём engine/
# session_factory на настройки ЭТОГО теста (:memory:).
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.session as _dbs

_s = _gs()
_dbs.engine = create_async_engine(_s.database_url)
_dbs.session_factory = async_sessionmaker(
    _dbs.engine, class_=_dbs.AsyncSession, expire_on_commit=False)

# Bot нужен только как аннотация; файл использует `from __future__ import
# annotations`, но явный импорт убирает F821 при проверке типов/линтером.
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, SendChatAction
from aiogram.types import CallbackQuery, Chat, Message, Update, User

# loguru-перехватчик: фиксируем факт входа в хендлер
_captured_logs: list[str] = []


class FakeSession(BaseSession):
    """Заглушка HTTP-сессии Bot API: пишет все методы в record."""

    def __init__(self) -> None:
        super().__init__()
        self.record: list[tuple[str, dict]] = []

    async def close(self) -> None:  # pragma: no cover
        pass

    async def make_request(self, bot, method, timeout=None):
        # Сигнатура BaseSession.make_request в aiogram 3.x: (bot, method, timeout).
        # Методы Bot API — pydantic-модели; фиксируем имя и данные.
        name = type(method).__name__

        def _ser(v):
            # reply_markup — pydantic-модель (InlineKeyboardMarkup), приводим
            # к dict, чтобы тест мог инспектировать callback_data кнопок.
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
        return {
            "message_id": mid,
            "date": int(datetime.now().timestamp()),
            "chat": {"id": 42, "type": "private",
                     "first_name": "Test", "last_name": "User"},
            "from": {"id": 123456, "is_bot": True,
                     "first_name": "test", "username": "test_bot"},
            "text": data.get("text") or data.get("caption") or "",
        }

    async def stream_content(self, url, headers=None, timeout=30,
                             chunk_size=4096, raise_for_status=True):
        yield b""


def _build_update(bot: Bot) -> Update:
    chat = Chat(id=42, type="private", first_name="Test", last_name="User")
    msg = Message(message_id=1, date=datetime.now(), chat=chat)
    # ВАЖНО: в aiogram 3.x методы Message (edit_text/answer) работают только
    # когда объект «смонтирован» на конкретный Bot. Именно эта ошибка
    # («method is not mounted to a any bot instance») ломала рендер меню
    # мероприятий — воспроизводим её в тесте корректным монтированием.
    msg = msg.as_(bot)
    cb = CallbackQuery(
        id=str(uuid.uuid4()),
        from_user=User(id=42, is_bot=False, first_name="Test", last_name="User"),
        chat_instance=str(uuid.uuid4()),
        data="menu:events",
        message=msg,
    )
    return Update(update_id=1, callback_query=cb)


async def _run() -> None:
    from aiogram import Bot, Dispatcher

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

    engine = dbs.engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    dp = Dispatcher(storage=await probe_fsm_storage(
        _make_fsm_storage(get_settings().redis_url)))
    dp.update.outer_middleware(dbs.DbMiddleware())
    dp.callback_query.outer_middleware(errors.ErrorNotifyMiddleware())
    dp.include_routers(
        errors.error_router, admin.router, access_handlers.router,
        start.router, tracker.router, tamagotchi.router, games.router,
        shop.router, merch.router, events.router, social.router,
        arena.router, stats.router, settings_h.router,
    )
    dp.errors.register(errors.on_error)

    session = FakeSession()
    bot = Bot(token=get_settings().bot_token, session=session)

    await dp.feed_update(bot, _build_update(bot))

    # 1. Хендлер должен быть реально вызван (кнопка не «мёртвая»).
    assert any("handler entered" in m for m in _captured_logs), \
        "Обработчик menu:events не был вызван диспетчером"

    # 2. Кнопка не «висит»: callback answer'ится всегда.
    answered = [d for m, d in session.record if m == "AnswerCallbackQuery"]
    assert answered, f"callback.answer() не вызван; record={session.record}"

    # 3. Пользователь видит экран мероприятий (заглушка для пустого списка).
    edits = [d for m, d in session.record if m == "EditMessageText"]
    sends = [d for m, d in session.record if m == "SendMessage"]
    texts = [str(d.get("text", "")) for d in edits + sends]
    stub = [t for t in texts if "Мероприятия канала" in t]
    assert stub, f"Экран мероприятий не отрендерен; texts={texts}"
    assert "Пока пусто" in stub[0], \
        "При отсутствии мероприятий должна показываться заглушка «Пока пусто»"

    # 4. Клавиатура живая: есть навигация «Меню».
    src = edits[0] if edits else sends[0]
    kb = json.dumps(src.get("reply_markup", {}), ensure_ascii=False)
    assert "menu:main" in kb, f"В клавиатуре нет кнопки Меню: {kb}"

    # 5. Кнопки в клавиатуре НЕ задублированы (as_markup() должен вызываться
    #    ровно один раз; иначе Telegram отклоняет edit с BUTTON_DATA_INVALID).
    assert kb.count("menu:events") <= 1, \
        f"Дублирование кнопок в разметке: {kb}"

    print("FUNCTIONAL TEST OK: menu:events -> handler called, "
          "answer sent, empty-list stub rendered")


async def _run_repeat_press() -> None:
    """Повторное нажатие «Мероприятия» при уже открытом экране.

    Telegram отвечает BadRequest «message is not modified». Это НЕ должно
    приводить ни к какой видимой поломке: callback answer'ится, из исходного
    сообщения ничего не вычищается (клавиатура остаётся на месте).
    """
    from aiogram import Bot, Dispatcher

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

    engine = dbs.engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    dp = Dispatcher(storage=await probe_fsm_storage(
        _make_fsm_storage(get_settings().redis_url)))
    dp.update.outer_middleware(dbs.DbMiddleware())
    dp.callback_query.outer_middleware(errors.ErrorNotifyMiddleware())
    # Роутеры модулей уже «прикреплены» к первому Dispatcher (модули синглтоны)
    # — отвязываем их перед повторной регистрацией во втором диспетчере.
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

    class NotModifiedSession(FakeSession):
        async def make_request(self, bot, method, timeout=None):
            name = type(method).__name__
            if name == "EditMessageText":
                from aiogram.exceptions import TelegramBadRequest
                raise TelegramBadRequest(method, {
                    "ok": False, "error_code": 400,
                    "description": "Bad Request: message is not modified"})
            return await super().make_request(bot, method, timeout)

    session = NotModifiedSession()
    bot = Bot(token=get_settings().bot_token, session=session)

    upd = _build_update(bot)
    # первое нажатие падает на not-modified, второе — проверяем отсутствие
    # edit_reply_markup(None) (удаления клавиатуры) и наличие ответа на callback
    await dp.feed_update(bot, upd)

    names = [m for m, _ in session.record]
    answered = [d for m, d in session.record if m == "AnswerCallbackQuery"]
    assert answered, f"При not-modified callback не answer'нут: {names}"
    markup_clears = [d for m, d in session.record
                     if m == "EditMessageReplyMarkup"
                     and not d.get("reply_markup")]
    assert not markup_clears, \
        f"Хендлер стёр клавиатуру сообщения: {markup_clears}"
    print("REPEAT-PRESS TEST OK: not-modified handled silently, keyboard kept")


def test_events_menu_functional():
    from loguru import logger
    logger.remove()
    logger.add(lambda m: _captured_logs.append(str(m)), level="DEBUG")
    asyncio.run(_run())


def test_events_menu_repeat_press_not_modified():
    from loguru import logger
    logger.remove()
    logger.add(lambda m: _captured_logs.append(str(m)), level="DEBUG")
    asyncio.run(_run_repeat_press())


if __name__ == "__main__":
    test_events_menu_functional()
