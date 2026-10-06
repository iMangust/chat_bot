"""Функциональные тесты пяти исправлений раздела «🐾 Питомец».

1) Экран «🎒 Инвентарь» не содержит кнопку «🎒 Инвентарь» (открывает сам
   себя — «кнопка ничего не делает»).
2) Кормление/игра/мытьё на 100% показателя блокируются — нельзя накручивать
   XP вхолостую (feed/play/wash возвращают succeeded=False и не начисляют XP).
3) Тренировки — строго по кулдауну COOLDOWN_TRAIN_SEC (180 сек): вторая
   тренировка сразу после первой отклоняется даже при «бесплатных попытках».
4) После тренировки пользователь остаётся на экране тренировок (сообщение
   редактируется текстом «🏋️ Тренировки …»), а не выкидывается в хаб.
5) В хабе есть кнопка «📋 Статус», она открывает подробную карточку:
   показатели, характеристики, снаряжение по слотам, бонусы экипировки.

Сеть до Telegram подменена фейковой сессией; БД — sqlite в памяти.
Запуск из каталога bot/:  pytest tests/test_pet_screen_fixes_functional.py -v
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

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.session as _dbs

_s = _gs()

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy.pool import StaticPool


def _fresh_engine():
    return create_async_engine(_s.database_url, poolclass=StaticPool,
                               connect_args={"check_same_thread": False})


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.record: list[tuple[str, dict]] = []

    async def close(self) -> None:  # pragma: no cover
        pass

    async def make_request(self, bot: Bot, method, timeout=None):
        name = getattr(method, "__name__", type(method).__name__)
        m = getattr(method, "model", None)
        if m is not None:
            name = getattr(m, "__name__", name)
        payload = method.model_dump() if hasattr(method, "model_dump") else {}
        self.record.append((name, payload))
        return {"ok": True, "result": {
            "message_id": 1, "date": 0,
            "chat": {"id": payload.get("chat_id", 1), "type": "private"},
        }}

    async def stream_content(self, url, headers=None, chunk_size=65536):
        yield b""


def _detach(router):
    parent = getattr(router, "_parent_router", None)
    if parent is not None and router in parent.sub_routers:
        parent.sub_routers.remove(router)
    router._parent_router = None
    for child in list(router.sub_routers):
        child._parent_router = None
    router.sub_routers.clear()


def _build_update(bot: Bot, data: str, text: str = "") -> Update:
    u = User(id=42, is_bot=False, first_name="Test")
    chat = Chat(id=42, type="private")
    msg = Message(message_id=1, date=datetime.now(), chat=chat).as_(bot)
    cbq = CallbackQuery(id=str(uuid.uuid4()), from_user=u,
                        chat_instance=str(uuid.uuid4()),
                        data=data, message=msg)
    return Update(update_id=1, callback_query=cbq)


async def _prepare_db(*, gear: dict | None = None):
    from app.db.models import Base, Item, Pet, PetInventory
    from app.db.models import User as UserModel

    engine = _fresh_engine()
    _dms = async_sessionmaker(engine, expire_on_commit=False)
    _dbs.engine = engine
    _dbs.session_factory = _dms
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with _dms() as s:
        s.add(UserModel(tg_id=42, first_name="Test", username="test"))
        pet = Pet(user_id=42, name="Тест", species="cat", level=5, xp=100,
                  hunger=100.0, happiness=100.0, energy=90.0, hygiene=100.0,
                  health=100.0, strength=7, agility=4, intellect=3)
        if gear:
            ex = dict(pet.settings_extra or {})
            ex["gear"] = gear
            pet.settings_extra = ex
        s.add(pet)
        bread = Item(code="food_bread", name="Хлеб", icon="🍞", type="food",
                     price=5, effect={"hunger": 15}, description="")
        s.add(bread)
        await s.flush()
        s.add(PetInventory(pet_id=pet.id, item_id=bread.id, quantity=1))
        await s.commit()


async def _press(data: str, routers=None, fake=None):
    from app.handlers import shop as shop_h
    from app.handlers import tamagotchi as tg_h

    dp = Dispatcher()
    regs = routers or [tg_h.router, shop_h.router]
    for r in regs:
        _detach(r)
        dp.include_router(r)
    async with _dbs.session_factory() as session:
        fake = fake or FakeSession()
        bot = Bot(token=_s.bot_token, session=fake)
        await dp.feed_update(bot, _build_update(bot, data), session=session)
    texts = [str(d.get("text", ""))
             for m, d in fake.record
             if m.lower() in ("editmessagetext", "sendmessage")]
    kb_dump = str(fake.record)
    return fake, texts, kb_dump


# ---------------------------------------------------------------------------
# 1) Инвентарь: без кнопки «🎒 Инвентарь» внутри самого себя
# ---------------------------------------------------------------------------

def test_inventory_screen_has_no_self_button():
    """На экране инвентаря нет callback 'pet:inv' — кнопка не открывает сама себя."""
    async def run():
        await _prepare_db()
        _fake, texts, kb_dump = await _press("pet:inv")
        joined = "\n".join(texts)
        assert "Инвентарь" in joined, f"экран инвентаря не показан: {texts}"
        assert "pet:inv" not in kb_dump, \
            f"в клавиатуре инвентаря осталась кнопка «🎒 Инвентарь» (самопетля): {kb_dump[:900]}"
    asyncio.run(run())


def test_empty_inventory_screen_has_no_self_button():
    """ПУСТОЙ инвентарь тоже без самопетли.

    Раньше пустой экран рендерился вкладкой «🎒 Вещи» целиком — а на ней
    живёт кнопка входа «🎒 Инвентарь» (pet:inv). Пользователь видел кнопку
    «Инвентарь» внутри самого инвентаря. Тест с предметами этого не ловил,
    потому что заполненный экран использует paged_keyboard().
    """
    async def run():
        await _prepare_db()
        from sqlalchemy import delete

        from app.db.models import PetInventory
        async with _dbs.session_factory() as s:
            await s.execute(delete(PetInventory))
            await s.commit()
        _fake, texts, kb_dump = await _press("pet:inv")
        joined = "\n".join(texts)
        assert "Инвентарь пуст" in joined, f"пустой экран не показан: {texts}"
        assert "pet:inv" not in kb_dump, \
            f"на пустом инвентаре есть кнопка «🎒 Инвентарь» (самопетля): {kb_dump[:900]}"
    asyncio.run(run())


def test_use_item_returns_to_inventory_without_self_button():
    """После применения предмета — возврат на экран инвентаря без pet:inv."""
    async def run():
        await _prepare_db()
        fake, _, _ = await _press("pet:inv")  # кладём pet:inv в стек навигации
        from sqlalchemy import select

        from app.db.models import Item, Pet, PetInventory
        async with _dbs.session_factory() as s:
            # Страж состояний блокирует применение спящему/гуляющему —
            # моделируем «бодрствующего дома», иначе use_ok вернёт alert.
            pet = (await s.execute(select(Pet).where(Pet.user_id == 42))).scalar_one()
            pet.is_sleeping = False
            pet.walk_until = None
            row = (await s.execute(
                select(PetInventory).order_by(PetInventory.item_id).limit(1)
            )).scalar_one_or_none()
            if row is None:
                await s.commit()
                return
            item_id = row.item_id
            item = await s.get(Item, item_id)
            await s.commit()
        await _press(f"use:{item_id}", fake=fake)          # окно подтверждения
        fake, texts, kb_dump = await _press(f"use_ok:{item_id}", fake=fake)
        assert any(item.name in t for t in texts), f"предмет не применён: {texts}"
        assert "pet:inv" not in kb_dump, \
            f"после применения в клавиатуре снова «🎒 Инвентарь»: {kb_dump[:900]}"
    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2) Действия на 100% заблокированы (анти-абьюз XP)
# ---------------------------------------------------------------------------

def test_full_stats_block_feed_play_wash():
    from app.services.tamagotchi import TamagotchiService

    async def run():
        await _prepare_db()
        async with _dbs.session_factory() as s:
            from sqlalchemy import select

            from app.db.models import Pet
            svc = TamagotchiService(s)
            pet = (await s.execute(select(Pet).where(Pet.user_id == 42))).scalar_one()
            pet.hunger, pet.happiness, pet.hygiene = 100.0, 100.0, 100.0
            xp_before = pet.xp or 0

            res, fed = await svc.feed(pet, {"hunger": 15}, with_result=True)
            assert not fed, f"кормление на 100% сытости прошло: {res!r}"
            res, played = await svc.play(pet, True, with_result=True)
            assert not played, f"игра на 100% счастья прошла: {res!r}"
            res, washed = await svc.wash(pet, with_result=True)
            assert not washed, f"мытьё на 100% гигиены прошло: {res!r}"
            assert (pet.xp or 0) == xp_before, \
                "XP всё равно начислился за бесполезные действия"
    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3) Тренировки: жёсткий перерыв 180 секунд
# ---------------------------------------------------------------------------

def test_train_cooldown_180s_strict():
    from app.services.tamagotchi import COOLDOWN_TRAIN_SEC, TamagotchiService

    async def run():
        assert COOLDOWN_TRAIN_SEC == 180
        await _prepare_db()
        async with _dbs.session_factory() as s:
            from sqlalchemy import select

            from app.db.models import Pet
            svc = TamagotchiService(s)
            pet = (await s.execute(select(Pet).where(Pet.user_id == 42))).scalar_one()
            pet.energy, pet.hunger = 100.0, 60.0  # ресурсы есть, кулдауна нет

            first, ok1 = await svc.train(pet, "strength", with_result=True)
            assert ok1, f"первая тренировка не прошла: {first!r}"
            second, ok2 = await svc.train(pet, "strength", with_result=True)
            assert not ok2, \
                f"вторая тренировка прошла без перерыва (абьюз): {second!r}"
            third, ok3 = await svc.train(pet, "intellect", with_result=True)
            assert not ok3, "смена стат обходит перерыв между тренировками"
    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4) После тренировки остаёмся на экране тренировок
# ---------------------------------------------------------------------------

def test_after_train_stays_on_train_screen():
    async def run():
        await _prepare_db()
        # Первая тренировка (pet:train:strength) должна оставить пользователя
        # на экране тренировок: финальное сообщение содержит заголовок экрана.
        _fake, texts, _kb = await _press("pet:train:strength")
        final = texts[-1] if texts else ""
        assert "Тренировки" in final, \
            f"после тренировки выкинуло из экрана: {final[:300]!r}"
        # Кулдаун активен — повторный тап тоже должен остаться на экране
        # тренировок (с таймером), а не уводить в хаб питомца.
        _fake2, texts2, _kb2 = await _press("pet:train:agility")
        final2 = texts2[-1] if texts2 else ""
        assert "Тренировки" in final2, \
            f"тап по тренировке на кулдауне уводит в хаб: {final2[:300]!r}"
    asyncio.run(run())


# ---------------------------------------------------------------------------
# 5) Кнопка «📋 Статус» + подробная карточка
# ---------------------------------------------------------------------------

def test_hub_has_status_button():
    async def run():
        await _prepare_db()
        _fake, _texts, kb_dump = await _press("pet:page:0")
        assert "pet:status" in kb_dump, \
            f"в хабе питомца нет кнопки «Статус»: {kb_dump[:900]}"
    asyncio.run(run())


def test_status_screen_shows_gear_and_stats():
    async def run():
        await _prepare_db(gear={"weapon": "⚔️"})
        _fake, texts, _kb = await _press("pet:status")
        joined = "\n".join(texts)
        assert "Клинок ветерана" in joined, \
            f"снаряжение не показано в статусе: {joined[:600]!r}"
        assert "Оружие" in joined, "слоты снаряжения не подписаны"
        assert "Сила" in joined and "Показатели" in joined, \
            f"нет подробных характеристик: {joined[:600]!r}"
    asyncio.run(run())
