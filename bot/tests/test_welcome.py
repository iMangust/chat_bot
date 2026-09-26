"""Приветствия: регистрация подписчика через /start, ровно одно DM."""
from __future__ import annotations

from _helpers import FakeBot, get_settings, inspect, pytest, SimpleNamespace

from app.db.repositories import SubscriberRepository

# ---------------------------------------------------------------------------
# 3. Приветствия
# ---------------------------------------------------------------------------
class TestWelcome:
    @pytest.mark.asyncio
    async def test_add_pending_subscriber_idempotent(self, session):
        """Повтор не плодит строк; pending остаётся «свежим», welcomed — нет."""
        assert await SubscriberRepository(session).count() == 0
        added = await _add(session, 10)
        assert added is True
        # welcome-доставка не прошла (ЛС закрыты) → повторный сигнал обязан
        # вернуть True, иначе «вечный pending» так и не будет доставлен
        again = await _add(session, 10)
        assert again is True
        assert await SubscriberRepository(session).count() == 1
        # после успешного приветствия повторных DM не будет
        repo = SubscriberRepository(session)
        await repo.mark_welcomed(10)
        await session.commit()
        assert await _add(session, 10) is False

    @pytest.mark.asyncio
    async def test_reset_welcome(self, session):
        await _add(session, 11)
        repo = SubscriberRepository(session)
        await repo.mark_welcomed(11)
        await session.commit()
        row = await repo.get(11)
        assert row.welcomed_at is not None
        await repo.reset_welcome(11)
        row = await repo.get(11)
        assert row.welcomed_at is None

    @pytest.mark.asyncio
    async def test_welcome_pending_once(self, session, monkeypatch):
        from app.handlers import welcome
        st = get_settings()
        monkeypatch.setattr(st, "channel_username", "testchan")
        monkeypatch.setattr(st, "welcome_channel_enabled", True)
        await _add(session, 12)
        sent = []

        class Bot(FakeBot):
            async def send_message(self, uid, text, **k):
                sent.append(uid)
        async def _subscribed(*a, **k):
            return True
        from app.middlewares import gate
        monkeypatch.setattr(gate, "is_channel_subscribed", _subscribed)
        n = await welcome.welcome_pending_subscribers(Bot(), session)
        assert n == 1 and sent == [12]
        n2 = await welcome.welcome_pending_subscribers(Bot(), session)
        assert n2 == 0, "ровно одно приветствие"

    def test_start_registers_subscriber(self):
        src = inspect.getsource(__import__("app.handlers.start", fromlist=["cmd_start"]).cmd_start)
        assert "add_pending_subscriber" in src, "/start должен ставить в очередь приветствий"

    def test_start_does_not_send_channel_welcome(self):
        """Регресс v1.5.7: /start НЕ должен слать канальное приветствие —
        иначе новичок получал два DM подряд (приветствие + онбординг)."""
        src = inspect.getsource(__import__("app.handlers.start", fromlist=["cmd_start"]).cmd_start)
        assert "welcome_pending_subscribers" not in src
        assert "reset_welcome" not in src

    @pytest.mark.asyncio
    async def test_no_double_welcome_after_restart(self, session, monkeypatch):
        """Приветствованный пользователь не возвращается в pending-очередь
        ни через /start-сигнал (add без reset), ни после рестарта бота."""
        from app.handlers import welcome as w
        st = get_settings()
        monkeypatch.setattr(st, "channel_username", "testchan")
        monkeypatch.setattr(st, "welcome_channel_enabled", True)
        async def _subscribed(*a, **k):
            return True
        from app.middlewares import gate
        monkeypatch.setattr(gate, "is_channel_subscribed", _subscribed)
        sent: list[int] = []
        class Bot(FakeBot):
            async def send_message(self, uid, text, **k):
                sent.append(uid)
        # первое событие вступления → одно приветствие
        await _add(session, 77)
        assert await w.welcome_pending_subscribers(Bot(), session) == 1
        assert sent == [77]
        # «косвенные» сигналы после этого (/start, сообщение в чате) и
        # catch-up после рестарта не должны приветствовать заново
        for chat in (-100, -999, -55):
            await _add(session, 77, chat=chat)
        assert await w.welcome_pending_subscribers(Bot(), session) == 0
        assert sent == [77]

    @pytest.mark.asyncio
    async def test_join_second_tracked_chat_rewelcomes_once(self, session):
        """Реальное вступление в ДРУГОЙ отслеживаемый чат (reset_welcome=True)
        даёт ровно одно дополнительное приветствие, повтор того же чата — нет."""
        repo = SubscriberRepository(session)
        await _add(session, 78)
        await repo.mark_welcomed(78)
        await session.commit()
        # тот же чат ещё раз — сброса нет
        assert await _add(session, 78, reset=True) is False
        # другой чат — сброс и ожидание приветствия
        assert await _add(session, 78, chat=-200, reset=True) is True
        row = await repo.get(78)
        assert row.welcomed_at is None and row.chat_id == -200
        # пока приветствие не доставлено, повторные события не плодят сбросы:
        # запись уже pending — add вернёт True без изменения welcomed_at
        assert await _add(session, 78, chat=-300, reset=True) is True
        assert (await repo.get(78)).welcomed_at is None
        # после доставки welcome-очередь закрывается, и тот же чат больше
        # не возвращает пользователя в pending
        await repo.mark_welcomed(78)
        await session.commit()
        assert await _add(session, 78, chat=-200, reset=True) is False

    @pytest.mark.asyncio
    async def test_channel_and_group_membership_single_welcome(self, session, monkeypatch):
        """Регресс v1.5.10: участник канала И группы получал по welcome-DM
        на каждое chat_member-событие (сброс welcomed_at при «другом чате»).
        Теперь приветствие ради конкретного чата запоминается
        (welcome_sent_chat_id), и возврат в него не сбрасывает отметку."""
        from app.handlers import welcome as w
        st = get_settings()
        monkeypatch.setattr(st, "channel_username", "testchan")
        monkeypatch.setattr(st, "welcome_channel_enabled", True)
        from app.middlewares import gate
        async def _subscribed(*a, **k):
            return True
        monkeypatch.setattr(gate, "is_channel_subscribed", _subscribed)
        sent: list[int] = []
        class Bot(FakeBot):
            async def send_message(self, uid, text, **k):
                sent.append(uid)
        repo = SubscriberRepository(session)

        def _cm_event(chat_id: int, user_id: int = 90):
            user = SimpleNamespace(id=user_id, is_bot=False,
                                   first_name="N", username=None)
            return SimpleNamespace(
                chat=SimpleNamespace(id=chat_id, type="channel" if chat_id == -100 else "supergroup"),
                new_chat_member=SimpleNamespace(user=user, status="member"))

        # событие из канала → ровно одно приветствие
        await w.on_channel_join(_cm_event(-100), Bot(), session)
        assert sent == [90], f"первое вступление должно дать одно DM, got {sent}"
        # то же событие дублируется/повторяется — нового приветствия нет
        await w.on_channel_join(_cm_event(-100), Bot(), session)
        assert sent == [90], "повтор канала — без второго DM"
        # событие из группы: человек УЖЕ зарегистрирован здесь (chat_id записи
        # = группа после первого flush? нет — запись осталась на канале).
        # Первое вступление в группу формально даёт право на ещё одно DM —
        # но если он был пойман сканом/трекером как участник группы ранее
        # (chat_id записи = группа), возврата в pending нет:
        await _add(session, 91, chat=-555)          # трекер: пишет в группе
        await w.welcome_pending_subscribers(Bot(), session)
        before = len(sent)
        await w.on_channel_join(_cm_event(-555, user_id=91), Bot(), session)
        assert len(sent) == before, \
            "вступление в чат, где пользователь уже учтён, не должно приветствовать заново"
        # и даже явное повторное событие там же — тишина
        await w.on_channel_join(_cm_event(-555, user_id=91), Bot(), session)
        assert len(sent) == before
        row = await repo.get(91)
        assert row.welcomed_at is not None


async def _add(session, uid, chat=-100, reset=False):
    from app.handlers.welcome import add_pending_subscriber
    return await add_pending_subscriber(session, uid, chat, first_name="N",
                                        reset_welcome=reset)
