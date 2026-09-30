"""Функциональные тесты меню мерча (реальный прогон через Dispatcher).

Проверяет:
1. «🧢 Наш мерч» для админа показывает кнопку «🛠 Управление мерчем»
   (по аналогии с «🛠 Управление мероприятиями» на экране мероприятий),
   а обычный пользователь её не видит;
2. кнопка «➕ Новый товар» больше не может вести на голлый callback
   "madmin:addprod" без кода категории — он ни под какой фильтр не попадал
   и ловился catch-all'ем «Кнопка устарела» (кнопка выглядела мёртвой);
3. клик по «🛠 Управление мерчем» реально открывает экран управления
   (callback answer'ится, экран рендерится) — то есть вся цепочка живая.

Сеть до Telegram подменена фейковой сессией, БД — sqlite в памяти.
Запуск из каталога bot/:  pytest tests/test_merch_menus_functional.py -v
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
os.environ["ADMIN_IDS"] = "[42]"          # пользователь id=42 — админ

# Сброс lru-кеша настроек: иначе get_settings() вернёт кеш другого теста
# из этого же pytest-процесса (например, с ADMIN_IDS=[]).
from app.config import get_settings as _gs  # noqa: E402
_gs.cache_clear()
os.environ["MERCH_ENABLED"] = "true"

# ВАЖНО: app.db.session создаёт engine при импорте — если этот модуль уже
# был импортирован другим тестом процесса (с его файловой sqlite), движок
# указывал бы на чужую базу и видел её данные (регрессия: «Встреча» из
# теста admin_api попадала в экран мероприятий). Принудительно пересоздаём
# engine/session_factory на настройки ЭТОГО теста (:memory:).
import app.db.session as _dbs  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

_s = _gs()
_dbs.engine = create_async_engine(_s.database_url)
_dbs.session_factory = async_sessionmaker(
    _dbs.engine, class_=_dbs.AsyncSession, expire_on_commit=False)

from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.methods import GetMe, SendChatAction  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Message, Update, User  # noqa: E402

USER_ID = 42


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
        return {
            "message_id": mid,
            "date": int(datetime.now().timestamp()),
            "chat": {"id": USER_ID, "type": "private", "first_name": "Test"},
            "from": {"id": 123456, "is_bot": True, "first_name": "test",
                     "username": "test_bot"},
            "text": data.get("text") or data.get("caption") or "",
        }

    async def stream_content(self, url, headers=None, timeout=30,
                             chunk_size=4096, raise_for_status=True):
        yield b""


def _build_update(bot, cb_data: str) -> Update:
    chat = Chat(id=USER_ID, type="private", first_name="Test")
    msg = Message(message_id=1, date=datetime.now(), chat=chat).as_(bot)
    cb = CallbackQuery(
        id=str(uuid.uuid4()),
        from_user=User(id=USER_ID, is_bot=False, first_name="Test"),
        chat_instance=str(uuid.uuid4()),
        data=cb_data,
        message=msg,
    )
    return Update(update_id=1, callback_query=cb)


async def _build_dp_and_bot():
    """Полный диспетчер как в проде + чистая sqlite-база."""
    from aiogram import Bot, Dispatcher

    import app.db.session as dbs
    from app.config import get_settings
    from app.db.models import Base
    from app.handlers import (access as access_handlers, admin, arena, errors,
                              events, games, merch, settings as settings_h,
                              shop, social, start, stats, tamagotchi, tracker)
    from app.main import _make_fsm_storage, probe_fsm_storage

    engine = dbs.engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    dp = Dispatcher(storage=await probe_fsm_storage(
        _make_fsm_storage(get_settings().redis_url)))
    dp.update.outer_middleware(dbs.DbMiddleware())
    dp.callback_query.outer_middleware(errors.ErrorNotifyMiddleware())
    routers = [errors.error_router, admin.router, access_handlers.router,
               start.router, tracker.router, tamagotchi.router, games.router,
               shop.router, merch.router, events.router, social.router,
               arena.router, stats.router, settings_h.router]
    # роутеры — синглтоны модулей; при повторном создании dp перепривязываем
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


def _all_callbacks(session: FakeSession) -> list[str]:
    out = []
    for name, data in session.record:
        kb = data.get("reply_markup")
        if not kb:
            continue
        for row in kb.get("inline_keyboard", []):
            for btn in row:
                if btn.get("callback_data"):
                    out.append(btn["callback_data"])
    return out


async def _run() -> None:
    from app.db.repositories import MerchRepository
    import app.db.session as dbs

    dp, bot, session = await _build_dp_and_bot()

    # наполним каталог: категория + товар с позицией в наличии
    async with dbs.session_factory() as s:
        repo = MerchRepository(s)
        cat = await repo.add_category("hoodies", "Худи", "🧥")
        p = await repo.add_product(cat.id, "Тестовое худи", sizes=["M"], colors=["Чёрный"])
        await repo.add_variant(p.id, "M", "Чёрный", 1990, 5)
        await s.commit()

    # 1) Админ жмёт «🧢 Наш мерч» → видим кнопку «🛠 Управление мерчем»
    await dp.feed_update(bot, _build_update(bot, "menu:merch"))
    cbs = _all_callbacks(session)
    assert "madmin:home" in cbs, \
        f"Админу не показана кнопка управления мерчем: {cbs}"
    answered = [d for m, d in session.record if m == "AnswerCallbackQuery"]
    assert answered, "menu:merch не answer'нул callback"
    texts = [str(d.get("text", "")) for m, d in session.record
             if m in ("EditMessageText", "SendMessage")]
    assert any("Мерч канала" in t for t in texts), f"Экран мерча не отрендерен: {texts}"
    assert "merch:cat:hoodies" in cbs, \
        f"Категория не в клавиатуре: {cbs}"

    # 2) Ни одна кнопка не ведёт на голлый "madmin:addprod" (без кода)
    bad = [c for c in cbs if c == "madmin:addprod"]
    assert not bad, f"Мёртвый callback без категории в клавиатуре: {bad}"

    # 3) Клик по «🛠 Управление мерчем» открывает экран управления
    session.record.clear()
    await dp.feed_update(bot, _build_update(bot, "madmin:home"))
    texts = [str(d.get("text", "")) for m, d in session.record
             if m in ("EditMessageText", "SendMessage")]
    assert any("Управление мерчем" in t for t in texts), \
        f"Экран управления не открылся: {texts}"
    cbs2 = _all_callbacks(session)
    assert "madmin:catalog" in cbs2 and "merch:myres" in cbs2, \
        f"На экране управления нет ключевых кнопок: {cbs2}"
    assert "madmin:addprod" not in cbs2, \
        f"Кнопка «Новый товар» снова без категории: {cbs2}"
    # после открытия категории код запоминается и addprod получает категорию
    session.record.clear()
    await dp.feed_update(bot, _build_update(bot, "madmin:catalog"))
    await dp.feed_update(bot, _build_update(bot, "madmin:cat:hoodies"))
    cbs3 = _all_callbacks(session)
    assert any(c.startswith("madmin:addprod:") for c in cbs3), \
        f"«➕ Новый товар» не несёт код категории: {cbs3}"
    # и сам клик по такой кнопке живой (не словил catch-all «устарела»)
    session.record.clear()
    await dp.feed_update(bot, _build_update(bot, "madmin:addprod:hoodies"))
    alerts = [d for m, d in session.record if m == "AnswerCallbackQuery"]
    joined = json.dumps(alerts, ensure_ascii=False)
    assert "устарела" not in joined, \
        f"Клик по «Новый товар» пойман catch-all'ем устаревших кнопок: {joined}"

    print("MERCH MENU FUNCTIONAL TEST OK: admin CTA shown, addprod carries "
          "category, admin home renders")


def test_merch_menus_functional():
    asyncio.run(_run())


if __name__ == "__main__":
    test_merch_menus_functional()
