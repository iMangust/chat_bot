"""Функциональный тест кнопок главного меню: Награды/Статистика/Топ/Карточка.

Воспроизводит баг из продакшн-логов:
    WARNING | app.handlers.events - unhandled menu callback: 'menu:ach'
    WARNING | app.handlers.events - unhandled menu callback: 'menu:stats'
    WARNING | app.handlers.events - unhandled menu callback: 'menu:card'
    WARNING | app.handlers.events - unhandled menu callback: 'menu:top'

Причина: aiogram перебирает РОУТЕРЫ в порядке регистрации; catch-all
«menu:*» в роутере events (зарегистрирован раньше stats/social) перехватывал
коллбэки чужих роутеров, и кнопки отвечали «Кнопка устарела».

Проверяемые исправления:
 1) для menu:stats/menu:ach/menu:top/menu:card в events зарегистрированы
    «мосты» на настоящие экраны — коллбэк обрабатывается по месту;
 2) любой прочий непойманный «menu:*» не оставляет пользователя со старой
    клавиатурой: catch-all перерисовывает актуальное главное меню.

Сеть до Telegram подменена фейковой сессией; БД — sqlite в памяти.
Запуск из каталога bot/:  pytest tests/test_menu_buttons_functional.py -v
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
from aiogram.methods import GetMe, SendChatAction
from aiogram.types import CallbackQuery, Chat, Message, Update, User

_captured_logs: list[str] = []


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
        # ВАЖНО: aiogram сериализует ответ Telegram через TypeAdapter(Метод),
        # и поля должны соответствовать схеме (например, parse_mode — str или
        # None, а не объект Default(...)); иначе валидация ответа падает ещё
        # до попадания в хендлер. Возвращаем полный валидный Update-объект.
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


ROUTER_ORDER = [
    "errors.error_router", "admin.router", "access_handlers.router",
    "start.router", "tracker.router", "tamagotchi.router", "games.router",
    "shop.router", "merch.router", "manual.router", "events.router",
    "social.router", "arena.router", "stats.router", "settings_h.router",
]


async def _build_dp():
    """Диспетчер с той же регистрацией роутеров, что в main.py/runtime.py."""
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
        manual,
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
                  errors=errors, events=events, games=games, manual=manual,
                  merch=merch,
                  settings_h=settings_h, shop=shop, social=social,
                  start=start, stats=stats, tamagotchi=tamagotchi,
                  tracker=tracker)
    routers = []
    for spec in ROUTER_ORDER:
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


async def _press(data: str):
    from aiogram import Bot

    from app.config import get_settings
    dp = await _build_dp()
    # Пользователь id=42 должен существовать в БД — иначе экраны статистики/
    # карточки честно отвечают «Сначала нажми /start» (так и задумано).
    import app.db.session as dbs
    from app.db.repositories import UserRepository
    async with dbs.session_factory() as s_:
        await UserRepository(s_).get_or_create(42, "Test", "testuser")
    session = FakeSession()
    bot = Bot(token=get_settings().bot_token, session=session)
    await dp.feed_update(bot, _build_update(bot, data))
    texts = [str(d.get("text", ""))
             for m, d in session.record
             if m in ("EditMessageText", "SendMessage")]
    alerts = [d for m, d in session.record
              if m == "AnswerCallbackQuery" and d.get("show_alert")]
    return session, texts, alerts


def _assert_handled(data: str, marker: str) -> None:
    async def run():
        session, texts, _alerts = await _press(data)
        joined = "\n".join(texts)
        assert any(m == "AnswerCallbackQuery" for m, _ in session.record), \
            f"{data}: callback не answer'нут"
        assert "устарела" not in joined, \
            f"{data}: вместо экрана показан текст про устаревшую кнопку: {texts}"
        assert marker in joined, \
            f"{data}: не найден ожидаемый экран ({marker!r}); texts={texts}"
        # ловушка catch-all не должна срабатывать на живые кнопки
        assert not any("unhandled menu callback" in m for m in _captured_logs), \
            f"{data}: коллбэк ушёл в catch-all (регрессия бага из логов)"
    asyncio.run(run())


def test_menu_stats_button():
    _assert_handled("menu:stats", "Твоя статистика")


def test_menu_achievements_button():
    # заголовок экрана достижений в коде: «🏆 <b>Достижения</b>» (не «Награды»)
    _assert_handled("menu:ach", "Достижения")


def test_menu_top_button():
    _assert_handled("menu:top", "Топ")


def test_menu_card_button():
    # карточка отправляется фото с подписью «🪪 Твоя карточка игрока»,
    # поэтому проверяем и текст, и факт отправки фото
    async def run():
        session, texts, alerts = await _press("menu:card")
        assert any(m == "AnswerCallbackQuery" for m, _ in session.record), \
            f"callback не answer'нут: {session.record}"
        joined = "\n".join(texts)
        assert "устарела" not in joined, f"показан тост про устаревшую кнопку: {texts}"
        caps = [str(d.get("caption", "")) for m, d in session.record if m == "SendPhoto"]
        assert any("карточка" in c.lower() for c in caps), \
            f"карточка-фото не отправлено: photos={caps}, texts={texts}"
    asyncio.run(run())


def test_stale_button_self_heals_menu():
    """Неизвестный menu:* больше не оставляет старую клавиатуру:
    catch-all перерисовывает актуальный экран прямо в чате (edit или send)."""
    async def run():
        _captured_logs.clear()
        session, texts, alerts = await _press("menu:zzz_old")
        # тост «устарела» допустим как AnswerCallbackQuery (не-алерт), но
        # пользователю обязательно что-то отрендерено вместо мёртвой кнопки
        assert any(m in ("EditMessageText", "SendMessage")
                   for m, _ in session.record), \
            f"после устаревшей кнопки ничего не отрендерено: {session.record}"
        all_text = "\n".join(
            str(d.get("text", "")) + str(d.get("caption", ""))
            for m, d in session.record if m in ("EditMessageText", "SendMessage"))
        kb_dump = str(session.record)
        assert ("Меню" in all_text or "🐾" in all_text or "Выбери питомца" in all_text
                or "menu:main" in kb_dump), \
            f"актуальное меню/экран не показаны: {all_text[:200]}"
    asyncio.run(run())


def _setup_logger():
    from loguru import logger
    logger.remove()
    logger.add(lambda m: _captured_logs.append(str(m)), level="DEBUG")


def test_main():
    _setup_logger()
    test_menu_stats_button()
    test_menu_achievements_button()
    test_menu_top_button()
    test_menu_card_button()
    test_stale_button_self_heals_menu()
    print("MENU BUTTONS FUNCTIONAL TEST OK")


if __name__ == "__main__":
    test_main()
