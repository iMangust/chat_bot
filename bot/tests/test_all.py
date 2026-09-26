"""Единый тест-сьют TamaBot (v1.5.4).

Покрывает ключевые инварианты бота одной командой: pytest bot/tests.
Разделы:
  1. Навигация — пагинированное меню 2x3, ряд ◀️ i/n ▶️, один выход «🏠 Меню».
  2. Гейт доступа — только ЛС, только подписчики канала, ничего в чаты.
  3. Приветствия — регистрация подписчика через /start, ровно одно DM.
  4.питомец/экономика — базовые сервисы и репозитории на in-memory sqlite.
"""
from __future__ import annotations

import asyncio
import inspect
import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
import pytest_asyncio
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Chat, Message, User

from app.config import get_settings
from app.keyboards import inline as ikb
from app.keyboards.paged import paged_keyboard
from app.middlewares.gate import AccessGateMiddleware, is_channel_subscribed
from app.db.repositories import UserRepository, SubscriberRepository


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


# ---------------------------------------------------------------------------
# 1. Навигация
# ---------------------------------------------------------------------------
class TestNavigation:
    def test_main_menu_pages_defined(self):
        assert len(ikb.MENU_PAGES) >= 2
        for title, buttons in ikb.MENU_PAGES:
            assert title.startswith(("🎮", "👤"))
            assert 0 < len(buttons) <= 6

    def test_main_menu_two_per_row_and_one_exit(self):
        kb = ikb.main_menu(link="https://t.me/x", reward=50)
        for r in _content_rows(kb):
            assert len(r) <= 2, "контентные кнопки — строго по 2 в ряд"
        texts = [b.text for b in _btns(kb)]
        assert texts.count("🏠 Меню") == 1, "ровно один выход из меню"
        assert not any(t.startswith("⬅️") for t in texts), "дублей «Назад» быть не должно"

    def test_main_menu_nav_row_counter(self):
        total = len(ikb.MENU_PAGES)
        for page in range(total):
            kb = ikb.main_menu(link="l", reward=1, page=page)
            nav = [b for b in _btns(kb)
                   if (b.callback_data or "").startswith("menu:page:")]
            assert len(nav) == 2, "◀️ и ▶️ на месте"
            label = [b for b in _btns(kb) if (b.callback_data or "") == "menu:noop"]
            assert label and f"📖 {page + 1}/{total}" in label[0].text

    def test_main_menu_pagination_wraps(self):
        total = len(ikb.MENU_PAGES)
        kb = ikb.main_menu(link="l", reward=1, page=0)
        nav = [b.callback_data for b in _btns(kb)
               if (b.callback_data or "").startswith("menu:page:")]
        assert nav == [f"menu:page:{total - 1}", "menu:page:1"], \
            "зацикливание: ◀️ с первой страницы ведёт на последнюю"
        kb_last = ikb.main_menu(link="l", reward=1, page=total - 1)
        nav_last = [b.callback_data for b in _btns(kb_last)
                    if (b.callback_data or "").startswith("menu:page:")]
        assert nav_last == [f"menu:page:{total - 2}", "menu:page:0"]

    def test_merch_button_only_when_enabled(self):
        st = get_settings()
        was = getattr(st, "merch_enabled", True)
        try:
            st.merch_enabled = False
            kb = ikb.main_menu(link="l", reward=1, page=0)
            assert not any("мерч" in b.text.lower() for b in _btns(kb))
        finally:
            st.merch_enabled = was

    def test_pet_hub_navigation(self):
        kb = ikb.pet_hub(page=0)
        texts = [b.text for b in _btns(kb)]
        assert texts.count("🏠 Меню") == 1
        assert any("📖" in t for t in texts)
        for r in _content_rows(kb):
            assert len(r) <= 2

    def test_paged_keyboard_contract(self):
        from aiogram.types import InlineKeyboardButton as B
        items = [B(text=f"c{i}", callback_data=f"cb{i}") for i in range(12)]
        kb, page = paged_keyboard(items, title="🧪 Тест", prefix="t", page=0,
                                  page_size=4)
        btns = _btns(kb)
        assert page == 0
        assert any("📖 1/" in b.text for b in btns)
        assert sum(1 for b in btns if b.text == "🏠 Меню") == 1
        for r in _content_rows(kb):
            assert len(r) <= 2

    def test_no_double_back_in_all_keyboards(self):
        kbs = [ikb.main_menu(link="l", reward=1), ikb.pet_hub(page=0),
               ikb.back_to_main(), ikb.settings_keyboard({"daily_report": True})]
        for kb in kbs:
            texts = [b.text for b in _btns(kb)]
            assert texts.count("🏠 Меню") <= 1


# ---------------------------------------------------------------------------
# 2. Гейт доступа
# ---------------------------------------------------------------------------
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
        from app.middlewares import gate
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


# ---------------------------------------------------------------------------
# 4. База/экономика
# ---------------------------------------------------------------------------
class TestCore:
    @pytest.mark.asyncio
    async def test_user_get_or_create(self, session):
        repo = UserRepository(session)
        u = await repo.get_or_create(1001, "Ваня", "vanya")
        u2 = await repo.get_or_create(1001, "Ваня", "vanya")
        assert u.tg_id == u2.tg_id == 1001

    @pytest.mark.asyncio
    async def test_referrer_link(self, session):
        repo = UserRepository(session)
        await repo.get_or_create(2001, "A", None)
        await repo.get_or_create(2002, "B", None)
        assert await repo.set_referrer(2002, 2001) is True
        assert await repo.set_referrer(2002, 2001) is False  # однократно

    def test_version(self):
        from app.config import __version__
        assert __version__ == "1.5.17"

    def test_env_reading_windows_encodings(self, tmp_path, monkeypatch):
        """v1.5.13/1.5.14: .env в cp1251 / с BOM / битый — импорт не падает,
        короткие ключи API_ID/API_HASH/PHONE подхватываются (регресс Windows)."""
        import subprocess
        import sys
        from pathlib import Path
        env_dir = tmp_path / "bot"
        env_dir.mkdir()
        # cp1251-файл с кириллическим комментарием (как в Notepad на Windows)
        (env_dir / ".env").write_bytes(
            "# Привет, это конфиг бота\nAPI_ID=12345678\n"
            "API_HASH=0123456789abcdef0123456789abcdef\n".encode("cp1251"))
        code = (
            "import sys, os\n"
            "sys.path.insert(0, %r)\n"
            "from app.config import _read_env_values\n"
            "vals = _read_env_values(%r)\n"
            "assert vals['API_ID'] == '12345678', vals\n"
            "os.environ['API_ID'] = vals['API_ID']\n"
            "from app.config import _alias_short_mtproto_keys\n"
            "_alias_short_mtproto_keys()\n"
            "assert os.environ.get('TELEGRAM_API_ID') == '12345678'\n"
            "print('OK')"
        ) % (str(Path(__file__).resolve().parents[1]), str(env_dir / ".env"))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env={k: v for k, v in __import__("os").environ.items()
                                if k not in ("API_ID", "TELEGRAM_API_ID")})
        assert "OK" in r.stdout, (r.stdout, r.stderr)
        # BOM + utf-8
        (env_dir / ".env_bom").write_bytes(
            "\ufeffAPI_ID=999\n".encode("utf-8"))
        code2 = (
            "import sys; sys.path.insert(0, %r)\n"
            "from app.config import _read_env_values\n"
            "assert _read_env_values(%r)['API_ID'] == '999'\n"
            "print('OK')"
        ) % (str(Path(__file__).resolve().parents[1]), str(env_dir / ".env_bom"))
        r2 = subprocess.run([sys.executable, "-c", code2], capture_output=True, text=True)
        assert "OK" in r2.stdout, (r2.stdout, r2.stderr)
        #完全不 читаемый как текст мусор — не падает (latin-1 fallback)
        (env_dir / ".env_bin").write_bytes(b"\xff\xfe\x00garbage\xff")
        code3 = (
            "import sys; sys.path.insert(0, %r)\n"
            "from app.config import _read_env_values\n"
            "_read_env_values(%r)\n"
            "print('OK')"
        ) % (str(Path(__file__).resolve().parents[1]), str(env_dir / ".env_bin"))
        r3 = subprocess.run([sys.executable, "-c", code3], capture_output=True, text=True)
        assert "OK" in r3.stdout, (r3.stdout, r3.stderr)


# ---------------------------------------------------------------------------
# 5. Меню команд: регистрируется ТОЛЬКО в личных чатах (v1.5.8)
# ---------------------------------------------------------------------------
class TestCommandScope:
    @pytest.mark.asyncio
    async def test_commands_private_scope_only(self, tmp_path, monkeypatch):
        """on_startup ставит команды с scope AllPrivateChats и очищает
        глобальный/групповые scope'ы — в группах меню «/» должно быть пусто."""
        import os as _os

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


# ---------------------------------------------------------------------------
# 6. Фоновая активность без спама (v1.5.9)
# ---------------------------------------------------------------------------
class TestNoActivitySpam:
    async def test_sticker_message_no_instant_dm(self, session):
        """Зачёт стикера не должен слать НИ ОДНОГО мгновенного DM."""
        from app.db.models import NotificationQueue, User
        from app.services.activity import ActivityService

        u = User(tg_id=777, first_name="Sticker", onboarded=True)
        session.add(u)
        await session.flush()

        class ExplodingBot:
            async def send_message(self, *a, **k):
                raise AssertionError("мгновенный DM за сообщение недопустим (v1.5.9)")

        svc = ActivityService(session, bot=ExplodingBot())
        entry = await svc.process_group_message(
            user_id=777, chat_id=-1001, message_id=1, text=None,
            has_media=True, media_type="sticker", is_reply=False,
            mentions_count=0, is_command=False)
        assert entry is not None and entry.is_counted
        # ни одного уведомления в очереди тоже нет (XP/стикер — не «важное» событие)
        n = (await session.execute(
            select(func.count()).select_from(NotificationQueue))).scalar_one()
        assert n == 0

    async def test_levelup_goes_to_queue_not_instant(self, session, monkeypatch):
        """Левелап — важное событие: ОДНО сообщение в очередь, без мгновенного DM."""
        from app.db.models import NotificationQueue, User
        from app.services import activity as act_mod

        # понизим порог XP так, чтобы одно сообщение дало локап
        s = get_settings()
        monkeypatch.setattr(s, "xp_per_message", 10_000, raising=False)

        u = User(tg_id=888, first_name="Leveler", onboarded=True, level=1, xp=0)
        session.add(u)
        await session.flush()

        class ExplodingBot:
            async def send_message(self, *a, **k):
                raise AssertionError("мгновенный DM за левелап недопустим (v1.5.9)")

        svc = act_mod.ActivityService(session, bot=ExplodingBot())
        await svc.process_group_message(
            user_id=888, chat_id=-1001, message_id=2, text="hello world",
            has_media=False, media_type=None, is_reply=False,
            mentions_count=0, is_command=False)

        rows = list((await session.execute(select(NotificationQueue))).scalars())
        assert len(rows) == 1
        assert rows[0].kind == "levelup"
        assert rows[0].user_id == 888
        assert not rows[0].sent

    async def test_reaction_levelup_queued(self, session, monkeypatch):
        """Левелап от реакции — тоже через очередь, без прямого send_message."""
        from app.db.models import NotificationQueue, User
        from app.services.activity import ActivityService

        s = get_settings()
        monkeypatch.setattr(s, "xp_per_message", 1, raising=False)

        a = User(tg_id=901, first_name="Giver", onboarded=True, level=1, xp=99999)
        b = User(tg_id=902, first_name="Taker", onboarded=True, level=1, xp=0)
        session.add_all([a, b])
        await session.flush()

        class ExplodingBot:
            async def send_message(self, *a, **k):
                raise AssertionError("мгновенный DM за реакцию недопустим (v1.5.9)")

        svc = ActivityService(session, bot=ExplodingBot())
        ok = await svc.process_reaction(from_user=901, to_user=902,
                                        chat_id=-1001, message_id=5, emoji="❤️")
        assert ok is True
        kinds = [(r.user_id, r.kind) for r in
                 (await session.execute(select(NotificationQueue))).scalars()]
        assert (901, "levelup") in kinds

    def test_notify_instant_removed(self):
        """Старый спам-механизм удалён из кода полностью."""
        import inspect
        from app.services import activity as act_mod
        src = inspect.getsource(act_mod)
        assert "_notify_instant" not in src
        assert "Зачтено" not in src  # текст «Зачтено … — бонусные XP» больше не шлётся


# ---------------------------------------------------------------------------
# v1.5.11: MTProto-синхронизация подписчиков (опциональный Telegram API)
# ---------------------------------------------------------------------------
class TestMtprotoSync:
    async def test_last_seen_user_id_cursor(self, session):
        """Курсор «новых» = max(user_id); пустая база -> None."""
        from app.db.repositories import SubscriberRepository
        subs = SubscriberRepository(session)
        assert await subs.last_seen_user_id() is None
        await subs.add_if_new(500, -100123, first_name="Old")
        await subs.add_if_new(900, -100123, first_name="New")
        await session.commit()
        assert await subs.last_seen_user_id() == 900

    async def test_sync_delta_skips_old_users(self, session, monkeypatch):
        """Режим дельты заносит только id больше курсора (свежие регистрации)."""
        from app.services import mtproto_sync as mts
        from app.db.repositories import SubscriberRepository
        from app.config import get_settings

        st = get_settings()
        monkeypatch.setattr(st, "tracked_chat_ids", [-1001234567890], raising=False)
        monkeypatch.setattr(mts, "collect_participant_ids",
                            lambda: _coro([100, 500, 900, 1200]))
        subs = SubscriberRepository(session)
        await subs.add_if_new(900, -1001234567890, first_name="Known")
        await session.commit()

        # патчим фабрику сессий, чтобы sync работал поверх тестовой БД
        import sqlalchemy.ext.asyncio as aea
        class _Factory:
            def __call__(self):
                class _Ctx:
                    async def __aenter__(self): return session
                    async def __aexit__(self, *a): return False
                return _Ctx()
        monkeypatch.setattr(aea, "async_sessionmaker", lambda *a, **k: _Factory())
        monkeypatch.setattr(aea, "create_async_engine", lambda *a, **k: _Engine())
        class _Engine:
            async def dispose(self): pass
        monkeypatch.setattr("app.services.mtproto_sync.get_settings", lambda: st)

        res = await mts.sync_subscribers(first_run=False)
        # курсор = max(id в базе) = 900 ⇒ добавляется строго больше: только 1200
        assert res["added"] == 1
        for uid in (100, 500):
            assert await subs.get(uid) is None
        assert await subs.get(1200) is not None

    async def test_sync_first_run_adds_everyone(self, session, monkeypatch):
        from app.services import mtproto_sync as mts
        from app.db.repositories import SubscriberRepository
        from app.config import get_settings
        import sqlalchemy.ext.asyncio as aea

        st = get_settings()
        monkeypatch.setattr(st, "tracked_chat_ids", [-1001234567890], raising=False)
        monkeypatch.setattr(mts, "collect_participant_ids",
                            lambda: _coro([100, 200]))
        class _Factory:
            def __call__(self):
                class _Ctx:
                    async def __aenter__(self): return session
                    async def __aexit__(self, *a): return False
                return _Ctx()
        class _Engine:
            async def dispose(self): pass
        monkeypatch.setattr(aea, "async_sessionmaker", lambda *a, **k: _Factory())
        monkeypatch.setattr(aea, "create_async_engine", lambda *a, **k: _Engine())
        res = await mts.sync_subscribers(first_run=True)
        assert res["added"] == 2
        subs = SubscriberRepository(session)
        assert await subs.get(100) is not None and await subs.get(200) is not None

    def test_no_hardcoded_secrets(self):
        """Секретов в коде нет — только имена полей config и .env.example."""
        import inspect
        from app.services import mtproto_sync as mts
        src = inspect.getsource(mts)
        for frag in ("22406532", "8953437425", "github_pat", "AAE0h8"):
            assert frag not in src


def _coro(value):
    import asyncio
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    fut.set_result(value)
    return fut
