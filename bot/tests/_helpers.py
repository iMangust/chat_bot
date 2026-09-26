"""Общие хелперы тест-сьюта TamaBot (переиспользуются модулями tests/test_*).

Разбито из единого test_all.py (этап «чистота», v1.5.22).
"""
from __future__ import annotations

import asyncio  # noqa: F401 — реэкспорт для тестов (from _helpers import asyncio)
import inspect  # noqa: F401 — реэкспорт для тестов
from types import SimpleNamespace

import pytest  # noqa: F401 — реэкспорт для тестов
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import func, select  # noqa: F401 — реэкспорт для тестов

from app.config import get_settings  # noqa: F401 — реэкспорт для тестов
from app.middlewares.gate import AccessGateMiddleware


def _btns(kb):
    return [b for row in kb.inline_keyboard for b in row]


def _rows(kb):
    return kb.inline_keyboard


def _content_rows(kb):
    """Ряды без навигационного (◀️·i/n·▶️) и выхода (🏠 Меню)."""
    out = []
    for r in kb.inline_keyboard:
        texts = [b.text for b in r]
        if set(texts) <= {"◀️", "▶️"} or any(t.startswith("📖") or " 📖 " in t for t in texts):
            continue
        if texts == ["🏠 Меню"]:
            continue
        out.append(r)
    return out


def _coro(value):
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    fut.set_result(value)
    return fut


def _user(uid=1, bot_flag=False):
    return User(id=uid, is_bot=bot_flag, first_name="Test")


def _private_chat(cid=1):
    return Chat(id=cid, type=ChatType.PRIVATE)


def _group_chat(cid=-100):
    return Chat(id=cid, type=ChatType.SUPERGROUP)


def _message(chat, uid=1, text="hello", bot_flag=False):
    return Message(message_id=1, date=0, chat=chat,
                   from_user=_user(uid, bot_flag), text=text)


def _callback(chat, uid=1, data="menu:main"):
    msg = Message(message_id=1, date=0, chat=chat, from_user=_user(uid))
    return CallbackQuery(id="1", from_user=_user(uid), data=data,
                         chat_instance="ci", message=msg)


class _Patcher:
    """Контекстный патч атрибутов замороженных pydantic-объектов aiogram."""
    def __init__(self, obj, **attrs):
        self.obj, self.attrs = obj, attrs
    def __enter__(self):
        from unittest.mock import patch
        self._stack = []
        for k, v in self.attrs.items():
            stack = patch.object(type(self.obj), k, new=v)
            stack.start()
            self._stack.append(stack)
        return self
    def __exit__(self, *exc):
        for stack in self._stack:
            stack.stop()


def monkeypatched(obj, **attrs):
    return _Patcher(obj, **attrs)


class FakeBot:
    id = 999

    def __init__(self, statuses=("member",), raise_exc=None):
        self._statuses = list(statuses)
        self._raise = raise_exc

    async def get_chat_member(self, chat_id, user_id):
        if self._raise is not None:
            raise self._raise
        status = self._statuses.pop(0) if self._statuses else "member"
        return SimpleNamespace(status=status)


async def _run_gate(event, bot=None):
    """Прогон события через AccessGateMiddleware.

    Возвращает (result, data, answers): result == 'handled', если хендлер
    был вызван; answers — список аргументов event.answer(...) (заглушки
    вместо ответов в группах/неподписанным).
    """
    mw = AccessGateMiddleware()
    called = {"v": False}

    async def handler(ev, data):
        called["v"] = True
        return "handled"

    data = {"bot": bot or FakeBot()}
    answers = []

    async def fake_answer(*a, **k):
        answers.append(a[1:] if a and a[0] is event else a)

    with _Patcher(event, answer=fake_answer):
        res = await mw(handler, event, data)
    return ("handled" if called["v"] else res), data, answers
