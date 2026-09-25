"""Тесты приветствия новых подписчиков канала (v1.5.1)."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import ChannelSubscriber, User
from app.db.repositories import SubscriberRepository


@pytest.mark.asyncio
async def test_add_if_new_is_idempotent(session):
    repo = SubscriberRepository(session)
    assert await repo.add_if_new(111, -100123, "Ваня", "vanya") is True
    await session.commit()
    # повторная доставка апдейта / параллельный сигнал — не дубль
    assert await repo.add_if_new(111, -100123, "Ваня", "vanya") is False
    rows = (await session.execute(select(ChannelSubscriber))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_pending_and_mark_welcomed(session):
    repo = SubscriberRepository(session)
    await repo.add_if_new(222, -100123, "Петя")
    await session.commit()
    pend = await repo.pending_welcomes(limit=5)
    assert [p.user_id for p in pend] == [222]
    await repo.mark_welcomed(222)
    await session.commit()
    assert await repo.pending_welcomes(limit=5) == []
    assert await repo.count() == 1


def test_channel_welcome_text_default_has_placeholders():
    from app.handlers.welcome import channel_welcome_text
    text = channel_welcome_text()
    assert "{name}" in text          # подстановка имени обязательна
    assert "Начать" in text or "начать" in text.lower()


def test_chat_member_handler_registered():
    """router.welcome должен слушать chat_member — иначе Telegram не шлёт события."""
    from app.handlers.welcome import router
    assert router.chat_member.handlers, "нет обработчика chat_member"


def test_allowed_updates_include_chat_member():
    """chat_member должен быть в allowed_updates main и консоли."""
    import pathlib
    for rel in ("app/main.py", "app/console/runtime.py"):
        src = (pathlib.Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf8")
        assert '"chat_member"' in src, rel


@pytest.mark.asyncio
async def test_subscriber_registration_creates_user_stub(session):
    """После успешного приветствия пользователь заводится в базе (onboarded=False)."""
    users_repo = SubscriberRepository(session)
    await users_repo.add_if_new(333, -100123, "Саша", "sasha")
    await session.commit()
    sub = await session.get(ChannelSubscriber, 333)
    assert sub is not None and sub.welcomed_at is None
    # имитируем успех отправки: mark + get_or_create пользователя
    from app.db.repositories import UserRepository
    await UserRepository(session).get_or_create(333, "Саша", "sasha")
    await users_repo.mark_welcomed(333)
    await session.commit()
    u = await session.get(User, 333)
    assert u is not None and u.onboarded is False
