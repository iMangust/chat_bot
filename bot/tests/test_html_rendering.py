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
import glob
import os
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


# ---------- v1.4.2: регрессии на краш /start и error-handler ----------

def test_start_handlers_use_bare_html_name():
    """Все `html.escape(...)` в хендлерах должны ссылаться на импортированный
    модуль — иначе NameError при рендере (краш /start в 1.4.1)."""
    import ast as _ast
    import app
    for fn in glob.glob(os.path.join(os.path.dirname(app.__file__), "handlers", "*.py")):
        tree = _ast.parse(open(fn, encoding="utf-8").read())
        imported = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for a in node.names:
                    if a.name == "html" or a.name.startswith("html."):
                        imported.add(a.asname or "html")
        for node in _ast.walk(tree):
            # html.escape(...) где html — свободная переменная
            if (isinstance(node, _ast.Attribute) and isinstance(node.value, _ast.Name)
                    and node.value.id == "html" and node.attr == "escape"):
                assert "html" in imported, f"{os.path.basename(fn)}: html.escape без import html"


def test_main_menu_text_renders_with_weird_name():
    from app.handlers.start import _main_menu_text

    class U:
        first_name = "Mangust <b>&</b>"
        username = None
        level, xp, coins, streak_days = 1, 0, 10, 1
        pet_id, language = None, "ru"

    text = _main_menu_text(U())
    assert "&lt;b&gt;&amp;" in text          # имя экранировано
    assert "<b>" in text                      # own-разметка жива


def test_channel_anonymous_reaction_not_counted(patch_tracker):
    """v1.4.3: анонимная реакция (user=None, actor_chat задан) не должна
    начисляться конкретному юзеру — Telegram не раскрывает автора."""
    upd = _mk_update([], ["❤️"])
    upd.user = None
    upd.actor_chat = SimpleNamespace(id=-1001)
    asyncio.run(track_reaction_update(upd, session=None))
    assert _FakeActivityService.last_kwargs is None


def test_message_reaction_count_handler_registered():
    """v1.4.3: апдейты message_reaction_count должны разрешаться в
    allowed_updates и иметь хендлер (иначе бот их никогда не увидит)."""
    import inspect
    from aiogram import Dispatcher
    from app.handlers import tracker
    dp = Dispatcher()
    dp.include_router(tracker.router)
    used = set(dp.resolve_used_update_types())
    assert {"message_reaction", "message_reaction_count"} <= used
    # polling/webhook explicitly добавляют их к resolve_used_update_types
    main_src = inspect.getsource(__import__("app.main", fromlist=["main"]))
    assert '"message_reaction", "message_reaction_count"' in main_src
    runtime_src = inspect.getsource(__import__("app.console.runtime", fromlist=["runtime"]))
    assert '"message_reaction", "message_reaction_count"' in runtime_src


def test_on_error_accepts_aiogram_error_event():
    """aiogram шлёт ErrorEvent(update=..., exception=...) одной позиционной
    пачкой — on_error не должен падать с TypeError (маскировал первопричину)."""
    import asyncio
    from types import SimpleNamespace
    from app.handlers.errors import on_error

    ev = SimpleNamespace(update=SimpleNamespace(from_user=None),
                         exception=ValueError("boom"))
    result = asyncio.run(on_error(ev))
    assert result is True                     # событие погашено, дипсейчер жив
