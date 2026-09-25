"""Регресс-тесты v1.4.7: жизненный цикл питомца, статистика по типам, топ v2."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import ChatMessageLog, Pet, User
from app.db.repositories import ActivityRepository, UserRepository
from app.services import leaderboard as lb
from app.services.tamagotchi import TamagotchiService


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- критика/реанимация
class TestCriticalAndRevive:
    def setup_method(self):
        self.svc = TamagotchiService()

    def _pet(self, **stats) -> Pet:
        defaults = dict(user_id=1, name="Беляш", hunger=50, happiness=50,
                        energy=50, hygiene=50, health=50)
        defaults.update(stats)
        return Pet(**defaults)

    def test_not_critical_with_health(self):
        assert not self.svc.is_critical(self._pet(health=0, hunger=5))

    def test_critical_zero_health_and_stat(self):
        pet = self._pet(health=0, hunger=0)
        assert self.svc.is_critical(pet)

    def test_grace_period_after_revive(self):
        pet = self._pet(health=0, hunger=0)
        grace = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        pet.settings_extra = {"revive_grace_until": grace}
        assert not self.svc.is_critical(pet)  # в grace не смертельно

    def test_garbage_grace_treated_as_expired(self):
        pet = self._pet(health=0, hunger=0)
        pet.settings_extra = {"revive_grace_until": "не-дата"}
        assert self.svc.is_critical(pet)

    def test_revive_restores_stats_and_costs_growth(self):
        pet = self._pet(health=0, hunger=0)
        assert self.svc.revive_cost(pet) == 200
        _run(self.svc.revive(pet))
        assert pet.health >= 30 and pet.hunger >= 30
        assert not self.svc.is_critical(pet)          # спасён
        assert self.svc.revive_cost(pet) == 400       # вторая жизнь дороже

    def test_revive_lives_limit(self):
        pet = self._pet(health=0, hunger=0)
        pet.settings_extra = {"revives_used": TamagotchiService.MAX_REVIVES}
        assert self.svc.revive_cost(pet) == -1  # жизни кончились → только усыновление

    def test_free_newbie_revive_once(self):
        pet = self._pet(health=0, hunger=0)
        assert _run(self.svc.free_revive_for_newbie(pet)) is True
        assert self.svc.is_critical(pet) is False
        # повторная бесплатная — уже нет
        pet2 = self._pet(health=0, hunger=0)
        pet2.settings_extra = {"free_revive_used": 1}
        assert _run(self.svc.free_revive_for_newbie(pet2)) is False


# ---------------------------------------------------------------- архив/усыновление
@pytest.mark.asyncio()
async def test_adopt_archives_prev_and_bumps_generation(session):
    svc = TamagotchiService()
    user = User(tg_id=101, first_name="Mangust")
    session.add(user)
    await session.flush()
    old = Pet(user_id=101, name="Беляш", hunger=0, happiness=0, energy=0,
              hygiene=0, health=0)
    session.add(old)
    await session.flush()

    await svc.archive_pet(session, old, reason="rehomed")
    new = await svc.adopt_new(session, 101, "Комок", "cat")

    assert old.is_archived is True and old.archive_reason == "rehomed"
    assert new.generation == old.generation + 1
    assert new.is_archived is False
    hist = await svc.history(session, 101)
    assert [p.id for p in hist] == [old.id]


@pytest.mark.asyncio()
async def test_current_pet_queries_ignore_archived(session):
    """PetRepository.get_by_user фильтрует архив — старый питомец после
    «усыновления» не должен возвращаться в pet_hub/дуэлях/топах."""
    from app.db.repositories import PetRepository

    svc = TamagotchiService()
    session.add(User(tg_id=102, first_name="X"))
    old = Pet(user_id=102, name="Старик", hunger=50, happiness=50,
              energy=50, hygiene=50, health=50)
    session.add(old)
    await session.flush()

    repo = PetRepository(session)
    assert (await repo.get_by_user(102)).id == old.id  # до архива виден

    await svc.archive_pet(session, old, reason="rehomed")
    assert await repo.get_by_user(102) is None         # архив скрыт
    new = await svc.adopt_new(session, 102, "Малыш", "cat")
    assert (await repo.get_by_user(102)).id == new.id  # виден новый


# ---------------------------------------------------------------- статистика по типам
@pytest.mark.asyncio()
async def test_media_breakdown_groups_by_type_reply_mentions(session):
    repo = ActivityRepository(session)
    now = datetime.now(timezone.utc)
    rows = [
        dict(media_type="text", is_reply=False, mentions_count=0),
        dict(media_type="text", is_reply=True, mentions_count=2),
        dict(media_type="photo", is_reply=False, mentions_count=1),
        dict(media_type="sticker", is_reply=False, mentions_count=0),
        dict(media_type="game", is_reply=False, mentions_count=0),  # неизвестный тип → other
    ]
    for i, r in enumerate(rows):
        session.add(ChatMessageLog(user_id=201, chat_id=-1, message_id=100 + i,
                                   length=10, is_counted=True, created_at=now, **r))
    await session.flush()

    bd = await repo.media_breakdown(201)
    assert bd["text"] == 2 and bd["photo"] == 1 and bd["sticker"] == 1
    assert bd["other"] == 1
    assert bd["reply"] == 1 and bd["mentions"] == 3
    # за неделю тоже считается
    assert (await repo.media_breakdown(201, since=now - timedelta(days=7)))["text"] == 2


# ---------------------------------------------------------------- общий топ v2
@pytest.mark.asyncio()
async def test_overall_top_penalizes_single_category_champions(session):
    """Усреднённый рейтинг: всесторонне активный обходит чемпиона одной секции."""
    u_all = User(tg_id=301, first_name="Всяде", level=5, streak_days=5)
    u_one = User(tg_id=302, first_name="Однообразный", level=1, streak_days=0)
    u_mid = User(tg_id=303, first_name="Середняк", level=3, streak_days=3)
    session.add_all([u_all, u_one, u_mid])
    now = datetime.now(timezone.utc)
    # u_one — чемпион по сообщениям (50), но ноль во всём остальном
    for i in range(50):
        session.add(ChatMessageLog(user_id=302, chat_id=-1, message_id=1000 + i,
                                   length=20, is_counted=True, created_at=now))
    # u_all — понемногу везде: 30 сообщений
    for i in range(30):
        session.add(ChatMessageLog(user_id=301, chat_id=-1, message_id=2000 + i,
                                   length=20, is_counted=True, created_at=now))
    await session.flush()

    overall = await lb.overall_top(session, now - timedelta(days=1), limit=10)
    order = [u.tg_id for u, _s, _p in overall]
    assert order[0] == 301, f"всесторонний должен быть первым, получил {order}"
    # у односекционного чемпиона штраф за отсутствие в остальных секциях
    per_one = next(p for u, _s, p in overall if u.tg_id == 302)
    assert "talk" in per_one and len(per_one) < 6


def test_leaderboard_sections_include_overall_and_emotional():
    """Навигация топов v2 содержит новые страницы."""
    names = [s for s, _label in lb.TOP_SECTIONS] if hasattr(lb, "TOP_SECTIONS") else None
    # секции объявлены в handlers/stats.py — проверяем их наличие там
    import inspect
    from app.handlers import stats as stats_mod
    src = inspect.getsource(stats_mod)
    for needle in ("overall", "emotional", "karma"):
        assert needle in src, f"страницы топа '{needle}' нет в навигации"
    if names is not None:
        assert set(names) >= {"overall", "emotional"}
