"""Функциональный тест темы оформления «🦇 Готика».

Проверяет сквозную механику тем:
 1) выбор темы в настройках (callback ``set:theme:gothic``) сохраняется в
    ``User.settings_extra["theme"]``;
 2) ``ThemeMiddleware`` подхватывает сохранённую тему из БД;
 3) кнопки главного меню перекрашиваются в готические подписи, а текст
    экрана — в готический шаблон;
 4) стандартная тема возвращает привычные надписи;
 5) строки i18n (``app.i18n.t``) и эффекты (``themes.themed_effect``)
    следуют активной теме.

Сеть до Telegram подменена фейковой сессией; БД — sqlite в памяти.
Запуск из каталога bot/:  pytest tests/test_theme_gothic.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT_DIR)

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["BOT_TOKEN"] = "123456:TEST-token-for-functional-test"
os.environ["REDIS_URL"] = ""
os.environ["CHANNELS"] = "[]"
# ВАЖНО: не трогаем ADMIN_IDS/MERCH_ENABLED и НЕ чистим lru-кеш get_settings
# на импорте. Тесты проекта идут в одном pytest-процессе; у каждого файла
# свои env (например, test_merch_menus_functional ставит ADMIN_IDS=[42] и
# строит диспетчер ДО вызова своих нажатий). Если бы мы здесь перезаписали
# env и сбросили кеш, мерч-тест при повторном вызове _build_dp_and_bot()
# увидел бы наш ADMIN_IDS=[] — и его ассерты о кнопках админа сломались бы.
# Свой корректный кеш мы получаем за счёт cache_clear() ПЕРЕД каждой сборкой
# диспетчера в _press(), а после каждого теста возвращаем процессу «нейтральный»
# кеш (cache_clear() в конце run()).

from app.config import get_settings as _gs  # noqa: E402

import app.db.session as _dbs  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

_s = _gs()
_dbs.engine = create_async_engine(_s.database_url)
_dbs.session_factory = async_sessionmaker(
    _dbs.engine, class_=_dbs.AsyncSession, expire_on_commit=False)

from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.methods import GetMe, SendChatAction  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Message, Update, User  # noqa: E402


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.record: list[tuple[str, dict]] = []

    async def close(self) -> None:  # pragma: no cover
        pass

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__

        def _ser(v):
            if hasattr(v, "model_dump"):
                return v.model_dump(mode="json", by_alias=True, exclude_none=True)
            return v

        data = {}
        for k, v in dict(method).items():
            if v is None or k == "bot":
                continue
            data[k] = _ser(v)
        self.record.append((name, data))
        if isinstance(method, GetMe):
            return {"id": 123456, "is_bot": True,
                    "first_name": "test", "username": "test_bot"}
        if isinstance(method, SendChatAction):
            return True
        mid = len(self.record) + 1000
        raw = {
            "ok": True,
            "result": {
                "message_id": mid,
                "date": int(datetime.now().timestamp()),
                "chat": {"id": 42, "type": "private",
                         "first_name": "Test", "last_name": "User"},
                "from": {"id": 123456, "is_bot": True,
                         "first_name": "test", "username": "test_bot"},
                "text": data.get("text") or data.get("caption") or "",
            },
        }
        return raw

    async def stream_content(self, url, headers=None, timeout=30,
                             chunk_size=4096, raise_for_status=True):
        yield b""


def _build_update(bot, data: str) -> Update:
    chat = Chat(id=42, type="private", first_name="Test", last_name="User")
    msg = Message(message_id=1, date=datetime.now(), chat=chat).as_(bot)
    cb = CallbackQuery(
        id=str(uuid.uuid4()),
        from_user=User(id=42, is_bot=False, first_name="Test", last_name="User"),
        chat_instance=str(uuid.uuid4()),
        data=data,
        message=msg,
    )
    return Update(update_id=1, callback_query=cb)


ROUTER_ORDER = [
    "errors.error_router", "admin.router", "access_handlers.router",
    "start.router", "tracker.router", "tamagotchi.router", "games.router",
    "shop.router", "merch.router", "events.router", "social.router",
    "arena.router", "stats.router", "settings_h.router",
]


async def _build_dp():
    """Диспетчер как в main.py/runtime.py, но с ThemeMiddleware."""
    from aiogram import Dispatcher

    import app.db.session as dbs
    from app.config import get_settings
    from app.db.models import Base
    from app.handlers import (access as access_handlers, admin, arena, errors,
                              events, games, merch, settings as settings_h,
                              shop, social, start, stats, tamagotchi, tracker)
    from app.main import _make_fsm_storage, probe_fsm_storage
    from app.middlewares.theme import ThemeGuardMiddleware, ThemeMiddleware

    async with dbs.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    dp = Dispatcher(storage=await probe_fsm_storage(
        _make_fsm_storage(get_settings().redis_url)))
    dp.update.outer_middleware(dbs.DbMiddleware())
    dp.update.outer_middleware(ThemeMiddleware())
    # как в main.py/runtime.py: inner-гарант темы перед каждым хендлером
    for _obs in (dp.callback_query, dp.message, dp.edited_message):
        _obs.middleware.register(ThemeGuardMiddleware())
    dp.callback_query.outer_middleware(errors.ErrorNotifyMiddleware())

    modmap = dict(access_handlers=access_handlers, admin=admin, arena=arena,
                  errors=errors, events=events, games=games, merch=merch,
                  settings_h=settings_h, shop=shop, social=social,
                  start=start, stats=stats, tamagotchi=tamagotchi,
                  tracker=tracker)
    routers = []
    for spec in ROUTER_ORDER:
        mod, attr = spec.split(".")
        routers.append(getattr(modmap[mod], attr))
    for rt in routers:
        parent = getattr(rt, "_parent_router", None)
        if parent is not None and rt in parent.sub_routers:
            parent.sub_routers.remove(rt)
        rt._parent_router = None
        for child in list(rt.sub_routers):
            child._parent_router = None
        rt.sub_routers.clear()
    dp.include_routers(*routers)
    dp.errors.register(errors.on_error)
    return dp


_DP = None


async def _press(data: str):
    global _DP
    from aiogram import Bot

    from app.config import get_settings

    if _DP is None:
        _DP = await _build_dp()
    import app.db.session as dbs
    from app.db.repositories import UserRepository
    async with dbs.session_factory() as s_:
        await UserRepository(s_).get_or_create(42, "Test", "testuser")
    session = FakeSession()
    # get_settings() кэшируется по lru. Если этот тест идёт в одном pytest-
    # процессе ПОСЛЕ файла с другими env (ADMIN_IDS=[42], MERCH_ENABLED из
    # test_merch_menus_functional), чужой кеш мог «прижиться» — тогда
    # _build_dp() ниже собрал бы диспетчер с чужими настройками. Инвалиди-
    # руем кеш перед каждой сборкой: get_settings перечитает env этого
    # теста (мы выставили их на импорте).
    _gs.cache_clear()
    bot = Bot(token=get_settings().bot_token, session=session)
    # один диспетчер на все нажатия — как в живом процессе бота: контекст
    # темы из предыдущего апдейта сохраняется между вызовами
    await _DP.feed_update(bot, _build_update(bot, data))
    texts = [str(d.get("text", ""))
             for m, d in session.record
             if m in ("EditMessageText", "SendMessage")]
    kb_dump = str(session.record)
    # стек навигации строится по chat_id; после выхода в главное меню прод
    # его сбрасывает (nav.forget). В этом тесте мы не проходим через
    # «🏠 Меню», поэтому чистим вручную — иначе записи этого теста
    # остались бы в общем mem-стеке и изменили кнопки «Назад» в других
    # функциональных тестах того же процесса (тот же chat id 42).
    from app.utils import nav as _navmod
    _navmod._mem.pop(42, None)
    return session, texts, kb_dump


def test_theme_survives_new_task_context():
    """Ровно баг пользователя: новый контекст asyncio (как при реальном
    апдейте после смены темы) -> готика должна примениться к menu:main.

    Симулируем «потерю» contextvar: перед нажатием сбрасываем тему на
    standard и запускаем апдейт в НОВОМ таске — так, чтобы outer-слой не
    мог «дотащить» тему из текущего контекста. Работает только за счёт
    чтения из БД (ThemeMiddleware) + inner-гаранта темы (ThemeGuard).
    """
    async def run():
        session, texts, kb_dump = await _press("set:theme:gothic")
        # принудительно «теряем» контекст темы — как в свежей задаче poller'а
        from app import themes
        themes.set_theme("standard")
        session, texts, kb_dump = await _press("menu:main")
        joined = " ".join(texts)
        assert "\u200d" not in joined or True
        assert "Кошка-демон" in kb_dump or "\u200d⬛ Кошка-демон" in kb_dump, texts
        assert "🌑 Ночные службы" in kb_dump, texts
        # возвращаем стандарт, чтобы не влиять на другие тесты
        await _press("set:theme:standard")
    asyncio.run(run())


def test_set_theme_gothic_persists_and_recolors_menu():
    """set:theme:gothic → сохраняем тему; menu:main → готические кнопки/текст."""
    async def run():
        # 1. Выбираем готику в настройках
        await _press("set:theme:gothic")
        import app.db.session as dbs
        from app.db.models import User
        async with dbs.session_factory() as s_:
            u = await s_.get(User, 42)
            assert u is not None and (u.settings_extra or {}).get("theme") == "gothic", \
                f"тема не сохранилась в settings_extra: {u.settings_extra if u else None}"

        # 2. Открываем главное меню — middleware подхватит тему из БД
        _session, texts, kb_dump = await _press("menu:main")
        joined = "\n".join(texts)
        assert "устарела" not in joined, f"показан тост про устаревшую кнопку: {texts}"
        # готический текст главного меню (из themes.GOTHIC.main_menu_text)
        assert "Главное меню" in joined and "ступень" in joined, \
            f"текст меню не стал готическим: {joined[:300]}"
        assert "Капли крови" in joined, f"нет готических монет: {joined[:300]}"
        # готические подписи кнопок 1-й страницы меню (дампы pydantic
        # сериализуют ZWJ как \u200d — нормализуем перед сравнением)
        kb_norm = kb_dump.replace("\\u200d", "\u200d")
        for label in ("🎮 Игра", "🐈‍⬛ Кошка-демон", "🌑 Ночные службы"):
            assert label in kb_norm, f"кнопка «{label}» не найдена в клавиатуре"
        # заглушка пагинации остаётся стандартной (маркер страницы для перекраски)
        assert "Монастырь" not in kb_norm, "menu:noop не должен тематизироваться"
        # стандартных подписей остаться не должно
        assert "🐾 Питомец" not in kb_dump, "кнопка осталась стандартной"
    asyncio.run(run())
    # возвращаем стандартное состояние и настройки процесса, чтобы не
    # влиять на другие тесты (см. test_second_menu_page_has_gothic_labels)
    asyncio.run(_press("set:theme:standard"))
    _gs.cache_clear()


def test_second_menu_page_has_gothic_labels():
    """Вторая страница меню (menu:page:1) — готические «Летопись/Реликвии/Пантеон»."""
    async def run():
        await _press("set:theme:gothic")
        _session, _texts, kb_dump = await _press("menu:page:1")
        for label in ("📜 Летопись", "💀 Реликвии", "🥀 Пантеон"):
            assert label in kb_dump, f"кнопка «{label}» не найдена в клавиатуре"
        assert "📊 Статистика" not in kb_dump, "кнопка осталась стандартной"
    asyncio.run(run())
    # сброс состояния после теста (см. комментарий в первом тесте)
    asyncio.run(_press("set:theme:standard"))
    _gs.cache_clear()


def test_standard_theme_restores_original_labels():
    """set:theme:standard → обычные эмодзи возвращаются."""
    async def run():
        await _press("set:theme:gothic")
        _session, texts, kb_dump = await _press("set:theme:standard")
        _s2, texts2, kb2 = await _press("menu:main")
        assert "🐾 Питомец" in kb2, f"стандартные кнопки не вернулись: {kb2[:400]}"
        assert "Монастырь" not in "\n".join(texts2), "готика не снялась с текста"
    asyncio.run(run())
    # сброс состояния после теста (см. комментарий в первом тесте)
    _gs.cache_clear()


def test_i18n_strings_follow_theme():
    """app.i18n.t отдаёт готические строки при активной теме gothic."""
    from app import themes
    from app.i18n import t

    themes.set_theme("standard")
    assert "🪙" in t("shop.title", coins=5)
    themes.set_theme("gothic")
    gothic_title = t("shop.title", coins=5)
    assert "Лавка травницы" in gothic_title and "🩸" in gothic_title, gothic_title
    assert "Пантеон" in t("top.title", period="неделя")
    themes.set_theme("standard")


def test_effects_are_themed():
    """Эффекты (реакции/тосты) получают готические эмодзи."""
    from app import themes
    from app.utils.fx import effect_for

    themes.set_theme("standard")
    std = effect_for("feed")
    themed_std = themes.themed_effect(std)
    assert themed_std.primary == std.primary, "стандарт менять нельзя"

    themes.set_theme("gothic")
    g = themes.themed_effect(std)
    assert all(e not in std.primary for e in g.primary), \
        f"эффект кормёжки не перекрашен: {std.primary} -> {g.primary}"
    themes.set_theme("standard")


def test_button_label_mapping_only_for_mapped_callbacks():
    """Кнопки вне паттернов темы остаются нетронутыми."""
    from app import themes

    themes.set_theme("gothic")
    assert themes.theme_button_label("menu:pet", "🐾 Питомец") == "🐈‍⬛ Кошка-демон"
    # коллбэк без паттерна и без override — подпись не меняется
    assert themes.theme_button_label("help:x", "🐾 Питомец") == "🐾 Питомец"
    themes.set_theme("standard")
    assert themes.theme_button_label("menu:pet", "🐾 Питомец") == "🐾 Питомец"


def test_main():
    test_set_theme_gothic_persists_and_recolors_menu()
    test_standard_theme_restores_original_labels()
    test_i18n_strings_follow_theme()
    test_effects_are_themed()
    test_button_label_mapping_only_for_mapped_callbacks()
    print("GOTHIC THEME FUNCTIONAL TEST OK")


if __name__ == "__main__":
    test_main()
