"""Меню команд регистрируется ТОЛЬКО в личных чатах (v1.5.8)."""
from __future__ import annotations

from _helpers import FakeBot, get_settings, pytest

# ---------------------------------------------------------------------------
# 5. Меню команд: регистрируется ТОЛЬКО в личных чатах (v1.5.8)
# ---------------------------------------------------------------------------
class TestCommandScope:
    @pytest.mark.asyncio
    async def test_commands_private_scope_only(self, tmp_path, monkeypatch):
        """on_startup ставит команды с scope AllPrivateChats и очищает
        глобальный/групповые scope'ы — в группах меню «/» должно быть пусто."""

        monkeypatch.setenv("BOT_TOKEN", "123:fake-token-for-test")
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
        monkeypatch.setenv("REDIS_URL", "")
        get_settings.cache_clear()
        try:
            import app.main as main_mod

            calls = []

            class RecordingBot(FakeBot):
                async def delete_my_commands(self, scope=None, **kw):
                    calls.append(("delete", scope))
                    return True

                async def set_my_commands(self, commands, scope=None, **kw):
                    calls.append(("set", scope, list(commands)))
                    return True

            await main_mod.on_startup(RecordingBot())
        finally:
            get_settings.cache_clear()

        sets = [c for c in calls if c[0] == "set"]
        dels = [c for c in calls if c[0] == "delete"]
        # ровно одна установка команд — и только для ЛС
        assert len(sets) == 1
        from aiogram.types import BotCommandScopeAllPrivateChats
        assert isinstance(sets[0][1], BotCommandScopeAllPrivateChats)
        assert {c.command for c in sets[0][2]} >= {"start", "help"}
        # старые глобальные/групповые списки затёрты (иначе Telegram покажет
        # их в группах, т.к. setMyCommands без scope не сбрасывает скоупы)
        scopes_deleted = [c[1] for c in dels]
        assert any(s is None for s in scopes_deleted)          # глобальный
        from aiogram.types import BotCommandScopeAllGroupChats, BotCommandScopeAllChatAdministrators
        assert any(isinstance(s, BotCommandScopeAllGroupChats) for s in scopes_deleted)
        assert any(isinstance(s, BotCommandScopeAllChatAdministrators) for s in scopes_deleted)
