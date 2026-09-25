"""v1.4.1: весь исходящий HTML должен реально парситься Telegram'ом.

1) safe_edit_or_answer/answer_cb обязаны передавать parse_mode="HTML" —
   иначе aiogram шлёт None и клиент показывает сырые <b>...</b>.
2) Динамические имена (юзеры/питомцы) экранируются — символы <>& не должны
   ломать разметку.
3) Реакции засчитываются: message_reaction-апдейт доходит до
   ActivityService.process_reaction (добавление +, снятие — нет).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ParseMode
from aiogram.types import ReactionTypeEmoji

from app.handlers.tracker import _reaction_emoji, track_reaction_update
from app.services.leaderboard import leaderboard_text
from app.services.notifications import build_daily_report, build_streak_warning
from app.utils import safe_edit
from app.db.models import User


# ---------- 1. parse_mode в safe_edit ----------

def test_default_parse_mode_is_html():
    assert safe_edit.DEFAULT_PARSE_MODE == ParseMode.HTML


def _fake_text_message():
    m = AsyncMock()
    m.text = "old text"
    m.caption = None
    m.edit_text.return_value = m
    return m


def test_safe_edit_or_answer_passes_html_parse_mode():
    m = _fake_text_message()
    asyncio.run(safe_edit.safe_edit_or_answer(m, "<b>Топ</b>"))
    kwargs = m.edit_text.call_args.kwargs
    assert kwargs["parse_mode"] == ParseMode.HTML


def test_safe_edit_fallback_answer_has_parse_mode():
    m = _fake_text_message()
    m.text = None  # медиа без текста -> fallback на answer
    asyncio.run(safe_edit.safe_edit_or_answer(m, "<b>Топ</b>"))
    kwargs = m.answer.call_args.kwargs
    assert kwargs["parse_mode"] == ParseMode.HTML


def test_explicit_parse_mode_override_respected():
    m = _fake_text_message()
    asyncio.run(safe_edit.safe_edit_or_answer(m, "plain", parse_mode=None))
    assert m.edit_text.call_args.kwargs["parse_mode"] is None


# ---------- 2. экранирование имён ----------

def _user(**kw):
    u = SimpleNamespace(**kw)
    return u


def test_leaderboard_text_escapes_names():
    payload = {
        "messages": [[1, "<script>Вася</script>", 10]],
        "reactions": [[1, "A&B", 5]],
        "pets": [[1, "Мур&ка", 3, "<i>Хозяин</i>"]],
    }
    text = leaderboard_text(payload)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "A&amp;B" in text
    assert "&lt;i&gt;Хозяин&lt;/i&gt;" in text
    assert "<b>Итоги прошлой недели</b>" in text  # служебный HTML жив


def test_notifications_escape_names():
    u = _user(first_name="<b>Клингон</b>", streak_days=3)
    p = _user(first_name="Vasia & Co", streak_days=1)
    txt = asyncio.run(build_streak_warning(p))
    assert "<b>Vasia &amp; Co</b>" not in txt  # имя не должно стать тегом
    assert "Vasia &amp; Co" in txt
    rep = asyncio.run(build_daily_report(u, None, 5, rank=None))
    assert "&lt;b&gt;Клингон&lt;/b&gt;" in rep


# ---------- 3. зачёт реакций ----------

class _FakeActivityService:
    last_kwargs = None

    def __init__(self, session, bot=None):
        pass

    async def process_reaction(self, **kw):
        _FakeActivityService.last_kwargs = kw
        return True


def _mk_update(old_em, new_em, *, user_id=100, msg_author=200):
    def rl(emojis):
        return [ReactionTypeEmoji(type="emoji", emoji=e) for e in emojis]
    upd = SimpleNamespace(
        chat=SimpleNamespace(id=-1001, type="supergroup"),
        message_id=777,
        old_reaction=rl(old_em),
        new_reaction=rl(new_em),
        user=SimpleNamespace(id=user_id),
        actor_chat=None,
        bot=SimpleNamespace(get_message=AsyncMock(
            return_value=SimpleNamespace(from_user=SimpleNamespace(id=msg_author, is_bot=False)))),
    )
    return upd


@pytest.fixture()
def patch_tracker(monkeypatch):
    monkeypatch.setattr("app.handlers.tracker._is_tracked", lambda chat_id: True)
    import app.services.activity as act
    monkeypatch.setattr(act, "ActivityService", _FakeActivityService)
    # хендлер импортирует ActivityService по имени модуля — патчим там же
    import app.handlers.tracker as tr
    monkeypatch.setattr(tr, "ActivityService", _FakeActivityService)
    _FakeActivityService.last_kwargs = None


def test_reaction_added_is_counted(patch_tracker):
    upd = _mk_update([], ["❤️"])
    asyncio.run(track_reaction_update(upd, session=None))
    kw = _FakeActivityService.last_kwargs
    assert kw is not None
    assert kw["from_user"] == 100 and kw["to_user"] == 200
    assert kw["emoji"] == "❤️"


def test_reaction_removed_not_counted(patch_tracker):
    upd = _mk_update(["❤️"], [])
    asyncio.run(track_reaction_update(upd, session=None))
    assert _FakeActivityService.last_kwargs is None


def test_reaction_swap_counts_only_added(patch_tracker):
    upd = _mk_update(["👍"], ["🔥"])
    asyncio.run(track_reaction_update(upd, session=None))
    assert _FakeActivityService.last_kwargs["emoji"] == "🔥"


def test_self_reaction_not_counted(patch_tracker):
    upd = _mk_update([], ["❤️"], user_id=200, msg_author=200)
    asyncio.run(track_reaction_update(upd, session=None))
    assert _FakeActivityService.last_kwargs is None


def test_custom_emoji_reaction_recognized():
    custom = SimpleNamespace(type="custom_emoji", custom_emoji_id="123")
    assert _reaction_emoji(custom) == "💎"
    assert _reaction_emoji(ReactionTypeEmoji(type="emoji", emoji="😁")) == "😁"
