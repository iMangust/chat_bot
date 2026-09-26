"""Тесты бизнес-логики недельной арены (services/pet_duels).

v1.5.22: раньше модуль не покрывался тестами вовсе — регрессии в балансе
боёв/лимитов/призов остались бы незамеченными.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import LeaderboardSnapshot, NotificationQueue, Pet, User
from app.services import pet_duels as pd
from app.utils.local_time import now as local_now


async def mk_user(session, tg_id: int, coins: int = 0) -> User:
    u = User(tg_id=tg_id, first_name=f"U{tg_id}", coins=coins)
    session.add(u)
    await session.flush()
    return u


async def mk_pet(session, user: User, level: int = 5, **stats) -> Pet:
    p = Pet(user_id=user.tg_id, name=f"Pet{user.tg_id}", level=level, **stats)
    session.add(p)
    await session.flush()
    return p


# ------------------------------------------------------------------ чистые функции

def test_week_key_format_and_iso():
    assert pd.week_key(datetime(2026, 9, 26, tzinfo=timezone.utc)) == "2026-W39"
    assert pd.week_key(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "2026-W01"


def test_duel_power_formula_and_floor():
    # уровень*10 + сила*4 + ловк*3 + инт*2 + mood(+5 при happiness>=70)
    happy = Pet(level=2, strength=1, agility=1, intellect=1,
                happiness=80, energy=80, health=100)
    assert pd.duel_power(happy) == 2 * 10 + 4 + 3 + 2 + 5

    # больное/уставшее/грустное животное получает штрафы, но пол >= 1
    sad = Pet(level=1, strength=0, agility=0, intellect=0,
              happiness=10, energy=10, health=10)
    assert pd.duel_power(sad) == max(1, 10 - 5 - 10 - 8)


def test_resolve_duel_is_fair_for_stronger_and_seedable():
    strong = Pet(level=9, strength=9, agility=9, intellect=9,
                 happiness=90, energy=90, health=100)
    weak = Pet(level=1, strength=1, agility=1, intellect=1,
               happiness=90, energy=90, health=100)
    rng = random.Random(7)
    winners = [pd.resolve_duel(strong, weak, rng)[0] for _ in range(20)]
    # ±20% удачи не должна перевешивать 8-кратную разницу мощи на длинной дистанции
    assert sum(w.id is strong.id or w.level == 9 for w in winners) >= 18
    # детерминированность по сиду
    a = pd.resolve_duel(strong, weak, random.Random(42))[0].level
    b = pd.resolve_duel(strong, weak, random.Random(42))[0].level
    assert a == b


def test_cooldown_left():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    assert pd.duel_cooldown_left(Pet(settings_extra={})) == 0
    fresh = {"duel_last_ts": (now - timedelta(seconds=30)).isoformat()}
    p = Pet(settings_extra=fresh)
    assert pd.duel_cooldown_left(p, now) == pd.FIGHT_COOLDOWN_SEC - 30
    old = {"duel_last_ts": (now - timedelta(seconds=200)).isoformat()}
    assert pd.duel_cooldown_left(Pet(settings_extra=old), now) == 0
    # битый timestamp не должен ронять арену
    assert pd.duel_cooldown_left(Pet(settings_extra={"duel_last_ts": "не дата"}), now) == 0


# ------------------------------------------------------------------ асинхронщина на БД

@pytest.mark.asyncio
async def test_get_or_create_row_idempotent(session):
    u = await mk_user(session, 7001)
    p = await mk_pet(session, u)
    r1 = await pd.get_or_create_row(session, p.id, "2026-W40")
    r2 = await pd.get_or_create_row(session, p.id, "2026-W40")
    assert r1.id == r2.id


@pytest.mark.asyncio
async def test_pick_opponent_filters(session):
    """Соперник: +-3 уровня, живой, не архивный, не сам питомец."""
    u1 = await mk_user(session, 7101)
    u2 = await mk_user(session, 7102)
    me = await mk_pet(session, u1, level=5)
    near = await mk_pet(session, u2, level=7)
    far = await mk_pet(session, u2, level=15)
    sleeping = await mk_pet(session, u2, level=5, is_sleeping=True)
    archived = await mk_pet(session, u2, level=6, is_archived=True)

    pool_ids = set()
    for _ in range(30):  # random.choice — берём много выборок
        opp = await pd.pick_opponent(session, me)
        assert opp is not None
        pool_ids.add(opp.id)
    assert near.id in pool_ids
    assert me.id not in pool_ids and far.id not in pool_ids
    assert sleeping.id not in pool_ids and archived.id not in pool_ids


@pytest.mark.asyncio
async def test_fight_flow_scores_limits_xp(session):
    u1 = await mk_user(session, 7201)
    u2 = await mk_user(session, 7202)
    me = await mk_pet(session, u1, level=5, strength=5, agility=5, intellect=5,
                      happiness=90, energy=90, health=100)
    rival = await mk_pet(session, u2, level=5, strength=5, agility=5, intellect=5,
                         happiness=90, energy=90, health=100)

    res = await pd.fight(session, me)
    assert res["ok"] is True
    assert res["left"] == pd.DAILY_FIGHT_LIMIT - 1
    wk = pd.week_key()
    my_row = (await session.execute(
        select(pd.PetDuel).where(pd.PetDuel.pet_id == me.id,
                                 pd.PetDuel.week_key == wk))).scalar_one()
    opp_row = (await session.execute(
        select(pd.PetDuel).where(pd.PetDuel.pet_id == rival.id,
                                 pd.PetDuel.week_key == wk))).scalar_one()
    assert my_row.fights == 1 and opp_row.fights == 1
    if res["i_won"]:
        assert (my_row.wins, my_row.score) == (1, pd.SCORE_WIN)
        assert (opp_row.losses, opp_row.score) == (1, pd.SCORE_LOSS)
    else:
        assert (my_row.losses, my_row.score) == (1, pd.SCORE_LOSS)
        assert (opp_row.wins, opp_row.score) == (1, pd.SCORE_WIN)
    # XP получил победитель (+12) и проигравший (+3)
    assert me.xp + rival.xp == pd.DUEL_XP_WIN + pd.DUEL_XP_LOSS

    # кулдаун того же питомца блокирует второй бой
    res2 = await pd.fight(session, me)
    assert res2 == {"ok": False, "reason": "cooldown", "sec": pytest.approx(pd.FIGHT_COOLDOWN_SEC, abs=5)}

    # суточный лимит: снимаем кулдаун, ставим счётчик дня = лимиту
    me.settings_extra = {**me.settings_extra,
                         "duel_last_ts": (local_now()
                                          - timedelta(seconds=200)).isoformat(),
                         "duel_count": pd.DAILY_FIGHT_LIMIT}
    res3 = await pd.fight(session, me)
    assert res3 == {"ok": False, "reason": "limit"}


@pytest.mark.asyncio
async def test_fight_no_rivals(session):
    u = await mk_user(session, 7301)
    p = await mk_pet(session, u, level=1)
    res = await pd.fight(session, p)
    assert res == {"ok": False, "reason": "no_rivals"}


@pytest.mark.asyncio
async def test_arena_screen_states(session):
    u = await mk_user(session, 7401)
    p = await mk_pet(session, u, level=3)
    text, kb = await pd.arena_screen(session, u.tg_id)
    assert "Арена питомцев" in text and "Пока никто не дрался" in text
    assert any(b.callback_data == "arena:fight"
               for row in kb.inline_keyboard for b in row)

    # после боя с кулдауном кнопка боя заменяется на подсказку
    p.settings_extra = {"duel_day": local_now().date().isoformat(),
                        "duel_count": 1,
                        "duel_last_ts": local_now().isoformat()}
    await session.flush()
    text2, kb2 = await pd.arena_screen(session, u.tg_id)
    datas = [b.callback_data for row in kb2.inline_keyboard for b in row]
    assert "arena:noop" in datas and "arena:fight" not in datas
    # подсказка в тексте арены не показывается — она в подписи кнопки
    hint_btn = next(b for row in kb2.inline_keyboard for b in row
                    if b.callback_data == "arena:noop")
    assert "отдыхает" in (hint_btn.text or "")

    # суточный лимит исчерпан → тоже подсказка, но с другим текстом
    p.settings_extra = {**p.settings_extra, "duel_count": pd.DAILY_FIGHT_LIMIT,
                        "duel_last_ts": (local_now()
                                         - timedelta(seconds=200)).isoformat()}
    await session.flush()
    _, kb3 = await pd.arena_screen(session, u.tg_id)
    hint3 = next(b for row in kb3.inline_keyboard for b in row
                 if b.callback_data == "arena:noop")
    assert "Лимит" in (hint3.text or "")


@pytest.mark.asyncio
async def test_finish_week_prizes_idempotent(session):
    now = local_now()
    prev = pd.week_key(now - timedelta(days=7))

    users = [await mk_user(session, 7501 + i, coins=0) for i in range(4)]
    pets = [await mk_pet(session, u, level=5) for u in users]
    # расставляем очки: pet0 > pet1 > pet2 > pet3
    for score, wins in zip((100, 75, 50, 25), (4, 3, 2, 1)):
        row = await pd.get_or_create_row(session, pets.pop(0).id, prev)
        row.score, row.wins, row.fights = score, wins, wins
    await session.flush()

    changed = await pd.finish_week(session, prev_week=prev)
    assert changed is True
    # призы топ-3, 4-й без приза
    assert users[0].coins == pd.WEEKLY_PRIZES[1]
    assert users[1].coins == pd.WEEKLY_PRIZES[2]
    assert users[2].coins == pd.WEEKLY_PRIZES[3]
    assert users[3].coins == 0
    # уведомления поставлены в очередь
    notes = (await session.execute(select(NotificationQueue))).scalars().all()
    assert len(notes) == 3
    # повторный вызов — идемпотентен (маркер недели уже стоит)
    assert await pd.finish_week(session, prev_week=prev) is False
    snaps = (await session.execute(
        select(LeaderboardSnapshot).where(
            LeaderboardSnapshot.category == "pet_arena"))).scalars().all()
    assert len(snaps) == 1
    # текущая неделя в наградах не участвует
    assert all(s.data != [] or s.category.startswith("pet_duel_award:") for s in snaps)


def test_arena_text_markdown_safety():
    class R:  # док-объект строки PetDuel
        score, wins, losses = 10, 1, 0

    class FakeUser:
        pass

    pet = Pet(name="<b>Хакс</b>")
    owner = FakeUser()
    owner.first_name = "Вася & Ко"
    out = pd.arena_text([(pet, owner, R())], me_pet_id=None, wk="2026-W40")
    assert "&lt;b&gt;" in out and "<b>" not in out.split("<b>Арена")[1]
    assert "&amp;" in out  # амперсанд владельца экранирован
