"""Функциональный тест экрана «🎒 Инвентарь» и подтверждения использования.

Требования пользователя:
 1) На кнопке предмета видно, ЧТО внутри и сколько: подпись вида
    «🍞 Хлеб x1» (раньше было «🎯 Использовать …» — на мобильных обрезалось
    до «Использовать», и содержимое было не видно).
 2) Тап по кнопке НИЧЕГО не тратит, а показывает всплывающее окно
    подтверждения («use:<id>» → экран с «✅ Использовать» / «⬅️ Назад»).
 3) Реальное применение предмета — только после подтверждения («use_ok:<id>»),
    при этом количество списывается.

Сеть до Telegram подменена фейковой сессией; БД — sqlite в памяти.
Запуск из каталога bot/:  pytest tests/test_inventory_confirm_functional.py -v
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

from app.config import get_settings as _gs  # noqa: E402
_gs.cache_clear()

import app.db.session as _dbs  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

_s = _gs()

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.methods import GetMe, SendChatAction  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Message, Update, User  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


def _fresh_engine():
    """Новый in-memory движок на тест: общее состояние не «светит» между запусками."""
    return create_async_engine(_s.database_url, poolclass=StaticPool,
                               connect_args={"check_same_thread": False})


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.record: list[tuple[str, dict]] = []

    async def close(self) -> None:  # pragma: no cover
        pass

    async def stream_content(self, url, headers=None, timeout=30,
                             chunk_size=65536, raise_for_status=True):
        # Абстрактный метод BaseSession — в тесте контент не скачивается.
        yield b""

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
        return raw


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


async def _prepare_db() -> int:
    """Пользователь + питомец + seeded-магазин; в инвентаре — 1× «Хлеб».

    Возвращает item_id хлеба (для callback_data «use:<id>»).
    """
    from app.db.models import Base, Pet, PetInventory, User
    from app.db.repositories import PetRepository
    from app.handlers.shop import seed_items
    from app.services.tamagotchi import local_now

    # Каждый тест получает чистую in-memory БД (свой движок), чтобы
    # состояние предыдущих тестов не «светило» (UNIQUE tg_id).
    _dbs.engine = _fresh_engine()
    _dbs.session_factory = async_sessionmaker(
        _dbs.engine, expire_on_commit=False)

    async with _dbs.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with _dbs.session_factory() as s:
        s.add(User(tg_id=42, username="testuser", coins=1000))
        await s.flush()
        assert await seed_items(s) > 0, "каталог товаров не засеян"
        from datetime import timezone as _tz
        pet = Pet(user_id=42, name="Тестик", species="cat",
                  last_update=local_now().astimezone(_tz.utc).replace(tzinfo=None),
                  hunger=80, happiness=70, hygiene=70, energy=80, health=100)
        await PetRepository(s).create(pet)
        from app.db.models import Item
        from sqlalchemy import select
        bread = (await s.execute(
            select(Item).where(Item.code == "food_bread"))).scalar_one()
        s.add(PetInventory(pet_id=pet.id, item_id=bread.id, quantity=1))
        await s.commit()
        return bread.id


async def _press(data: str):
    from app.handlers import shop

    dp = Dispatcher()
    parent = getattr(shop.router, "_parent_router", None)
    if parent is not None and shop.router in parent.sub_routers:
        parent.sub_routers.remove(shop.router)
    shop.router._parent_router = None
    for child in list(shop.router.sub_routers):
        child._parent_router = None
    shop.router.sub_routers.clear()
    dp.include_router(shop.router)
    # Сессия БД передаётся в хендлеры так же, как это делает DbMiddleware
    # в боевом коде (одна сессия на апдейт).
    async with _dbs.session_factory() as session:
        fake = FakeSession()
        bot = Bot(token=_s.bot_token, session=fake)
        await dp.feed_update(bot, _build_update(bot, data), session=session)
    texts = [str(d.get("text", ""))
             for m, d in fake.record
             if m in ("EditMessageText", "SendMessage")]
    kb_dump = str(fake.record)
    return fake, texts, kb_dump


def test_inventory_button_shows_item_and_quantity():
    """Кнопка предмета подписана «🍞 Хлеб x1», без обрезающего «Использовать»."""
    async def run():
        item_id = await _prepare_db()
        _session, texts, kb_dump = await _press("pet:inv")
        joined = "\n".join(texts)
        assert "Инвентарь" in joined, f"экран инвентаря не показан: {texts}"
        assert "🍞 Хлеб x1" in kb_dump, \
            f"подпись кнопки не содержит «Хлеб x1»: {kb_dump[:800]}"
        assert "Использовать 🍞" not in kb_dump, \
            "старая длинная подпись «Использовать …» вернулась — на мобильных обрезается"
        assert f"use:{item_id}" in kb_dump
    asyncio.run(run())


def test_tap_opens_confirmation_without_spending():
    """Тап по предмету → окно подтверждения; предмет НЕ списан."""
    async def run():
        from app.db.models import PetInventory
        from sqlalchemy import select
        item_id = await _prepare_db()
        _session, texts, kb_dump = await _press(f"use:{item_id}")
        joined = "\n".join(texts)
        assert "Использовать предмет?" in joined, \
            f"нет окна подтверждения: {texts}"
        assert "Хлеб" in joined and "Сытость" in joined, \
            f"в окне нет названия/эффекта предмета: {joined[:300]}"
        assert "use_ok" in kb_dump, f"нет кнопки подтверждения: {kb_dump[:800]}"
        async with _dbs.session_factory() as s:
            qty = (await s.execute(select(PetInventory.quantity))).scalars().all()
        assert qty == [1], f"предмет списался ДО подтверждения: {qty}"
    asyncio.run(run())


def test_use_ok_spends_item():
    """Подтверждение «use_ok» применяет предмет и списывает одну штуку."""
    async def run():
        from app.db.models import PetInventory
        from sqlalchemy import select
        item_id = await _prepare_db()
        _session, texts, _kb = await _press(f"use_ok:{item_id}")
        joined = "\n".join(texts)
        assert "Хлеб" in joined or "Сытость" in joined or "🍖" in joined, \
            f"результат применения не показан: {texts}"
        async with _dbs.session_factory() as s:
            rows = (await s.execute(select(PetInventory))).scalars().all()
        assert not rows, f"предмет не списан после подтверждения: {rows}"
    asyncio.run(run())


if __name__ == "__main__":
    test_inventory_button_shows_item_and_quantity()
    test_tap_opens_confirmation_without_spending()
    test_use_ok_spends_item()
    print("INVENTORY CONFIRM FUNCTIONAL TEST OK")
