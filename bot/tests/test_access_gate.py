"""Гейт доступа: только ЛС, только подписчики канала, ничего в чаты."""
from __future__ import annotations

from _helpers import FakeBot, _private_chat, _group_chat, _message, _callback, get_settings, inspect, pytest, SimpleNamespace, asyncio, _run_gate

from app.middlewares.gate import is_channel_subscribed

class TestAccessGate:
    def setup_method(self):
        from app.middlewares import gate
        gate._pos_cache.clear()
        gate._neg_cache.clear()
        gate._warned_no_admin.clear()

    def test_group_message_not_answered(self, monkeypatch):
        # команда в группе: гейт не пропускает её к командным хендлерам,
        # бот молчит (текстовые сообщения идут в безмолвный трекер)
        st = get_settings()
        monkeypatch.setattr(st, "channel_username", "testchan")
        m = _message(_group_chat(), text="/start")
        result, _, answers = asyncio.run(_run_gate(m))
        assert result != "handled", "команда в группе не доходит до хендлеров"
        assert not answers, "бот не должен ничего отвечать в группе"

    def test_group_callback_swallowed(self, monkeypatch):
        st = get_settings()
        monkeypatch.setattr(st, "channel_username", "testchan")
        cb = _callback(_group_chat())
        result, _, answers = asyncio.run(_run_gate(cb))
        assert result != "handled", "callback в группе не доходит до хендлеров"

    def test_private_unsubscribed_gets_stub(self, monkeypatch):
        st = get_settings()
        monkeypatch.setattr(st, "channel_username", "testchan")
        m = _message(_private_chat(), text="привет")
        bot = FakeBot(statuses=("left",))
        result, _, answers = asyncio.run(_run_gate(m, bot=bot))
        assert result != "handled"
        assert answers and "🔒" in answers[0][0], "неподписанному — заглушка в ЛС"

    def test_private_subscribed_passes(self, monkeypatch):
        st = get_settings()
        monkeypatch.setattr(st, "channel_username", "testchan")
        m = _message(_private_chat(), text="привет")
        result, data, _ = asyncio.run(_run_gate(m, bot=FakeBot(("administrator",))))
        assert result == "handled" and data.get("sub_granted")

    def test_failopen_when_channel_unset(self, monkeypatch):
        st = get_settings()
        monkeypatch.setattr(st, "channel_username", None)
        m = _message(_private_chat(), text="привет")
        result, _, _ = asyncio.run(_run_gate(m))
        assert result == "handled"

    def test_bots_ignored(self):
        m = _message(_private_chat(), uid=5, text="привет", bot_flag=True)
        result, _, _ = asyncio.run(_run_gate(m))
        assert result != "handled"

    def test_subscribe_cache_positive_only(self, monkeypatch):
        st = get_settings()
        monkeypatch.setattr(st, "channel_username", "testchan")
        bot = FakeBot(("member", "left"))
        assert asyncio.run(is_channel_subscribed(bot, 123)) is True
        assert asyncio.run(is_channel_subscribed(bot, 123)) is True  # из кэша
        assert asyncio.run(is_channel_subscribed(bot, 456)) is False  # без кэша — API

    def test_failopen_on_api_error(self, monkeypatch):
        """Сбой Telegram API не должен «глушить» бота в ЛС."""
        from aiogram.exceptions import TelegramAPIError
        st = get_settings()
        monkeypatch.setattr(st, "channel_username", "testchan")
        exc = TelegramAPIError(method="getChatMember", message="502 Bad Gateway")
        m = _message(_private_chat(), text="привет")
        result, _, answers = asyncio.run(_run_gate(m, bot=FakeBot(raise_exc=exc)))
        assert result == "handled", "при ошибке API доступ разрешён (fail-open)"
        assert not answers, "заглушка «🔒» показываться не должна"

    @pytest.mark.asyncio
    async def test_failopen_and_admin_notice_when_bot_not_admin(self, monkeypatch):
        """Forbidden (бот не админ канала) => fail-open + разовый тост админу."""
        from aiogram.exceptions import TelegramForbiddenError
        st = get_settings()
        monkeypatch.setattr(st, "channel_username", "testchan")
        monkeypatch.setattr(st, "admin_ids", [777])
        sent = []

        class NotAdminBot(FakeBot):
            async def get_chat_member(self, chat_id, user_id):
                raise TelegramForbiddenError(method="getChatMember",
                                             message="403: bot must be an administrator")
            async def send_message(self, chat_id, text, **kw):
                sent.append((chat_id, text))
                return SimpleNamespace(message_id=1)

        bot = NotAdminBot()
        m = _message(_private_chat(), text="привет")
        result, _, _ = await _run_gate(m, bot=bot)
        assert result == "handled", "без прав админа канала бот обязан оставаться отзывчивым"
        await asyncio.sleep(0.01)  # дать ensure_future отработать
        assert sent and sent[0][0] == 777 and "администратор" in sent[0][1]

        m2 = _message(_private_chat(), text="ещё")
        result2, _, _ = await _run_gate(m2, bot=bot)
        assert result2 == "handled"
        await asyncio.sleep(0.01)
        assert len(sent) == 1, "предупреждение админу — однократное на канал"

    def test_negative_result_short_cached_and_resettable(self, monkeypatch):
        st = get_settings()
        monkeypatch.setattr(st, "channel_username", "testchan")
        from app.middlewares import gate
        gate.reset_subscribe_cache()
        bot = FakeBot(("left",))
        assert asyncio.run(is_channel_subscribed(bot, 321)) is False
        assert asyncio.run(is_channel_subscribed(bot, 321)) is False  # из короткого кэша
        gate.reset_subscribe_cache(321)                               # «проверить» сбрасывает
        assert asyncio.run(is_channel_subscribed(FakeBot(("member",)), 321)) is True

    def test_tracked_chats_gate_membership_in_one_of(self, monkeypatch):
        """Подписка хотя бы на ОДИН из TRACKED_CHAT_IDS открывает доступ."""
        from app.middlewares import gate
        st = get_settings()
        monkeypatch.setattr(st, "channel_username", None)
        monkeypatch.setattr(st, "tracked_chat_ids", [-1001, -1002])
        gate.reset_subscribe_cache()
        # первый чат — left, второй — member: доступ должен быть открыт
        bot = FakeBot(("left", "member"))
        assert asyncio.run(gate.is_channel_subscribed(bot, 555)) is True
        gate.reset_subscribe_cache()
        # во всех чатах left — доступа нет
        bot2 = FakeBot(("left", "left"))
        assert asyncio.run(gate.is_channel_subscribed(bot2, 556)) is False
        gate.reset_subscribe_cache()

    def test_required_chats_fallback_to_single_channel(self, monkeypatch):
        from app.middlewares import gate
        st = get_settings()
        monkeypatch.setattr(st, "tracked_chat_ids", [])
        monkeypatch.setattr(st, "channel_chat_id", -100999)
        monkeypatch.setattr(st, "channel_username", "testchan")
        assert gate.required_chats() == [("-100999", "testchan")]
        monkeypatch.setattr(st, "channel_chat_id", None)
        assert gate.required_chats() == [("", "testchan")]
        monkeypatch.setattr(st, "channel_username", None)
        assert gate.required_chats() == []

    def test_welcome_flushes_on_group_join_event(self):
        """on_new_members сам доставляет канальное приветствие (не ждёт скан)."""
        src = inspect.getsource(__import__(
            "app.handlers.welcome", fromlist=["on_new_members"]).on_new_members)
        assert "welcome_pending_subscribers" in src

    def test_channel_welcome_not_tied_to_username(self):
        """Доставка приветствий не требует CHANNEL_USERNAME (хватит TRACKED_CHAT_IDS)."""
        src = inspect.getsource(__import__(
            "app.handlers.welcome", fromlist=["welcome_pending_subscribers"]
        ).welcome_pending_subscribers)
        assert "required_chats" in src
        assert "not st.channel_username" not in src

    def test_commands_private_only(self):
        """Каждый командный хендлер ограничен приватными чатами."""
        from app.handlers import start, stats, settings, social, arena, tamagotchi
        found = 0
        for mod in (start, stats, settings, social, arena, tamagotchi):
            src = inspect.getsource(mod)
            for line in src.splitlines():
                if "@router.message(Command" in line:
                    found += 1
                    assert 'F.chat.type == "private"' in line or "private_only" in line, line
        assert found >= 9, f"ожидали >=9 командных декораторов, нашли {found}"

    def test_welcome_sends_nothing_to_group(self, monkeypatch):
        from app.handlers import welcome
        src = inspect.getsource(welcome.on_new_members)
        assert "message.answer" not in src, "в группу писать нельзя"
