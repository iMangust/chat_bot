"""Тесты новых модулей: лидерборды, друзья питомцев, карточка профиля."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import (ChatMessageLog, LeaderboardSnapshot, NotificationQueue,
                           Pet, User, UserStat)
from app.services.leaderboard import (build_weekly_payload, leaderboard_text,
                                      snapshot_weekly, top_messages, top_pets)
from app.services.pet_social import (MAX_FRIENDS, are_friends, list_friends,
                                     make_friends, render_friend_list,
                                     suggest_friend, unfriend)


async def _mk_user(session, tg_id, name, msgs=0):
    u = User(tg_id=tg_id, first_name=name, onboarded=True, messages_count=msgs)
    session.add(u)
    return u


async def _mk_pet(session, user_id, name, level=1):
    p = Pet(user_id=user_id, name=name, level=level)
    session.add(p)
    await session.flush()
    return p


async def _log_msg(session, tg_id, when=None):
    session.add(ChatMessageLog(chat_id=-100, message_id=next(_mid),
                               user_id=tg_id, length=11,
                               is_counted=True, created_at=when or datetime.now(timezone.utc)))


_mid = iter(range(1, 10_000))


# ─── топы ────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_top_messages_period_filter(session):
    now = datetime.now(timezone.utc)
    await _mk_user(session, 1, "A")
    await _mk_user(session, 2, "B")
    await _log_msg(session, 1, now - timedelta(days=1))
    await _log_msg(session, 1, now - timedelta(days=1))
    await _log_msg(session, 2, now - timedelta(days=30))
    await session.commit()
    week = await top_messages(session, now - timedelta(days=7), 10)
    assert week and week[0][0].tg_id == 1 and week[0][1] == 2
    # неучтённые (is_counted=False) в топ не попадают
    session.add(ChatMessageLog(chat_id=-100, message_id=next(_mid), user_id=2,
                               length=1, is_counted=False))
    await session.commit()
    week2 = await top_messages(session, now - timedelta(days=7), 10)
    assert week2[0][1] == 2


@pytest.mark.asyncio
async def test_snapshot_weekly_rewards_and_idempotent(session):
    now = datetime.now(timezone.utc)
    u1 = await _mk_user(session, 1, "Champ")
    u2 = await _mk_user(session, 2, "Second")
    for _ in range(5):
        await _log_msg(session, 1, now - timedelta(days=8))
    for _ in range(3):
        await _log_msg(session, 2, now - timedelta(days=8))
    await session.commit()

    payload = await snapshot_weekly(session, now=now)
    assert payload and payload["messages"][0][0] == 1
    snap = (await session.get(User, 1))
    assert snap.coins == 500          # приз за 1 место
    u2b = await session.get(User, 2)
    assert u2b.coins == 250
    # очередь уведомлений пополнена
    nq = list((await session.execute(
        __import__("sqlalchemy").select(NotificationQueue))).scalars())
    assert len(nq) >= 2
    # повторный вызов той же недели — идемпотентен, награды не дублируются
    again = await snapshot_weekly(session, now=now)
    assert again is not None
    champ = await session.get(User, 1)
    assert champ.coins == 500
    snaps = list((await session.execute(
        __import__("sqlalchemy").select(LeaderboardSnapshot))).scalars())
    assert len(snaps) == 1


@pytest.mark.asyncio
async def test_leaderboard_text_renders(session):
    payload = {"messages": [[1, "A", 10]], "reactions": [[2, "B", 3]],
               "pets": [[1, "Barsik", 4, "A"]]}
    t = leaderboard_text(payload)
    assert "🥇" in t and "A" in t and "Barsik" in t


# ─── друзья питомцев ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_make_friends_bidirectional_and_limits(session):
    p1 = await _mk_pet(session, (await _mk_user(session, 10, "X") ).tg_id, "Pushok")
    p2 = await _mk_pet(session, (await _mk_user(session, 11, "Y")).tg_id, "Vasya")
    await session.commit()
    ok, msg = await make_friends(session, p1, p2)
    assert ok and "друзья" in msg
    await session.commit()
    assert await are_friends(session, p1.id, p2.id)
    assert await are_friends(session, p2.id, p1.id)   # взаимно
    fr1 = await list_friends(session, p1.id)
    fr2 = await list_friends(session, p2.id)
    assert [p.name for p in fr1] == ["Vasya"] and [p.name for p in fr2] == ["Pushok"]
    # повторная дружба отклоняется
    ok2, msg2 = await make_friends(session, p1, p2)
    assert not ok2 and "уже" in msg2
    # сам с собой
    ok3, _ = await make_friends(session, p1, p1)
    assert not ok3
    # лимит MAX_FRIENDS
    others = []
    for i in range(MAX_FRIENDS + 1):
        uid = 100 + i
        await _mk_user(session, uid, f"U{i}")
        others.append(await _mk_pet(session, uid, f"P{i}"))
    await session.commit()
    cnt_ok = 0
    for o in others:
        okc, _m = await make_friends(session, p1, o)
        if okc:
            cnt_ok += 1
            await session.commit()
    assert cnt_ok == MAX_FRIENDS - 1  # уже один друг есть
    # unfriend
    await unfriend(session, p1.id, p2.id)
    await session.commit()
    assert not await are_friends(session, p1.id, p2.id)


@pytest.mark.asyncio
async def test_suggest_friend_excludes_self_and_friends(session):
    p1 = await _mk_pet(session, (await _mk_user(session, 20, "A")).tg_id, "One", level=3)
    await session.commit()
    sug = await suggest_friend(session, p1)
    assert sug is None  # других питомцев нет
    p2 = await _mk_pet(session, (await _mk_user(session, 21, "B")).tg_id, "Two", level=4)
    await session.commit()
    sug = await suggest_friend(session, p1)
    assert sug is not None and sug.id == p2.id
    await make_friends(session, p1, p2)
    await session.commit()
    sug = await suggest_friend(session, p1)
    assert sug is None  # друг не предлагается


def test_render_friend_list():
    class P: name = "Barsik"; level = 1
    txt = render_friend_list(P(), [])
    assert "0/5" in txt and "Пока никого" in txt


# ─── карточка профиля ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_profile_card_png(session):
    pytest.importorskip("PIL")
    from app.services.profile_card import get_or_render_card, render_profile_card
    u = await _mk_user(session, 30, "Katya", msgs=42)
    await _mk_pet(session, u.tg_id, "Mirka", level=2)
    await session.commit()
    png = await render_profile_card(session, 30)
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"
    png2, changed = await get_or_render_card(session, 30)
    assert changed is True   # первый рендер — «изменилась»
    _, changed2 = await get_or_render_card(session, 30)
    assert changed2 is False  # второй — кэш версии
    missing = await render_profile_card(session, 9999)
    assert missing is None


# ---------------------------------------------------------------- v1.3 fixes
def test_fsm_storage_uses_resp2_protocol():
    """FSM-хранилище обязано запрашивать protocol=2 (иначе HELLO-ошибка)."""
    import inspect
    from app.main import _make_fsm_storage
    src = inspect.getsource(_make_fsm_storage)
    assert "protocol=2" in src


def test_redis_client_resp2_and_timeouts():
    """Клиент кулдаунов: RESP2 + короткие таймауты (не вешает бота)."""
    import inspect
    from app.utils import redis as ru
    src = inspect.getsource(ru.init_redis)
    assert "protocol=2" in src and "socket_timeout" in src


@pytest.mark.asyncio
async def test_set_cooldown_fallback_without_server(monkeypatch):
    """Без живого Redis set_cooldown работает через in-memory fallback."""
    from app.utils.redis import close_redis, init_redis, set_cooldown
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/0")  # заведомо мертвый
    from app.config import get_settings
    get_settings.cache_clear()
    init_redis()
    try:
        assert await set_cooldown("test:cd:fallback", 5) is True
        assert await set_cooldown("test:cd:fallback", 5) is False  # кулдаун сработал
    finally:
        await close_redis()
        get_settings.cache_clear()
