"""Регресс-тесты второго раунда исправлений (HTML в ачивках, рефералка, магазин)."""
from __future__ import annotations

import html as _html

import pytest
from sqlalchemy import select

from app.db.models import Achievement, Item, Pet, User
from app.handlers.shop import seed_items
from app.services.achievements import AchievementService, seed_achievements


# ---------- рендер достижений: экранирование + пагинация ----------

def test_render_achievements_escapes_and_paginates():
    from app.handlers.stats import _render_achievements

    class A:
        def __init__(self, i):
            self.id = i
            self.title = f"Тест <script>{i}</b>"
            self.description = "Написать 10 & 20 сообщений"
            self.icon = "🏅"
            self.is_hidden = False
            self.condition_value = 10

    class UR:
        progress = 3
        unlocked_at = None

    items = [(A(i), UR()) for i in range(20)]
    text, page, total = _render_achievements(items, page=0)
    assert total == 3 and page == 0
    # сырых пользовательских тегов нет — только наш собственный <b>
    assert "<script>" not in text
    assert _html.escape("Тест <script>0</b>") in text
    assert "&amp;" in text  # описание тоже экранировано
    text2, page2, _ = _render_achievements(items, page=5)  # выход за границы -> clamp
    assert page2 == 2
    assert "20/20" in text2 or "/20" in text2


async def _check_unlocked_first(session):
    await seed_achievements(session)
    u = User(tg_id=777, first_name="X", onboarded=True)
    session.add(u)
    await session.flush()
    svc = AchievementService(session)
    achs = list((await session.execute(select(Achievement))).scalars())
    target = achs[-1]  # последняя по id — разблокируем её
    await svc.unlock(target, 777)
    items = await svc.list_for_user(777)
    assert items[0][0].id == target.id, "разблокированная ачивка должна быть первой"


@pytest.mark.asyncio
async def test_list_for_user_unlocked_first_marker(session):
    await _check_unlocked_first(session)


# ---------- реферальная ссылка ведёт на канал ----------

def test_invite_link_points_to_channel(monkeypatch):
    from app.config import Settings
    from app.handlers import start as st

    s = Settings(bot_token="42:TEST", channel_username="MyCoolChannel")
    monkeypatch.setattr(st, "get_settings", lambda: s)
    link = st.invite_link_for(123)
    assert link == "https://t.me/MyCoolChannel?start=invite_123"

    s2 = Settings(bot_token="42:TEST", channel_username=None)
    monkeypatch.setattr(st, "get_settings", lambda: s2)
    assert st.invite_link_for(123) == ""


# ---------- магазин: сид без жёстких id + мерч ----------

@pytest.mark.asyncio
async def test_seed_items_no_merch(session):
    """Мерч вынесен в отдельный раздел — в таблицу Items он больше не сидится."""
    created = await seed_items(session)
    assert created > 0
    items = list((await session.execute(select(Item))).scalars())
    merch = [i for i in items if i.type == "merch"]
    assert not merch, "мерч больше не часть магазина питомца"
    codes = {i.code for i in items}
    assert "food_bread" in codes and "toy_ball" in codes


@pytest.mark.asyncio
async def test_atomic_coin_spend_blocks_double_buy(session):
    """UPDATE ... WHERE coins>=price не списывает монеты дважды."""
    from sqlalchemy import update

    u = User(tg_id=888, first_name="M", onboarded=True, coins=50)
    session.add(u)
    await session.flush()
    price = 30
    for _ in range(3):  # три «клика» подряд
        res = await session.execute(
            update(User).where(User.tg_id == 888, User.coins >= price)
            .values(coins=User.coins - price)
        )
        if res.rowcount == 0:
            break
    fresh = (await session.execute(select(User).where(User.tg_id == 888))).scalar_one()
    assert fresh.coins == 20  # списание произошло ровно один раз
