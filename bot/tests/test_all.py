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


async def _add(session, uid):
    from app.handlers.welcome import add_pending_subscriber
    return await add_pending_subscriber(session, uid, -100, first_name="N")


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
        assert __version__ == "1.5.6"
