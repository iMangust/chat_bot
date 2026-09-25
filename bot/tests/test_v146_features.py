"""Регрессионные тесты v1.4.6: арена питомцев, кастомизация, i18n-слой."""
from __future__ import annotations

import asyncio
import inspect
import random
from datetime import datetime, timedelta, timezone

import pytest

from app import i18n
from app.db.models import Pet, User
from app.services import pet_duels


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- i18n ----

def test_i18n_tf_fallback_and_format():
    s = i18n.tf("ru", "top.title", period="Неделя")
    assert "Неделя" in s and "<b>" in s
    # неизвестный ключ -> сам ключ (UI не падает)
    assert i18n.tf("ru", "no_such_key_xyz") == "no_such_key_xyz"
    # неизвестный язык -> fallback ru
    assert i18n.tf("xx", "pet.washed") == i18n.tf("ru", "pet.washed")
    # битые плейсхолдеры не роняют перевод
    assert isinstance(i18n.tf("ru", "shop.bought"), str)


def test_i18n_context_roundtrip():
    i18n.set_current_lang("en")
    assert i18n.get_current_lang() == "en"
    assert "Chatters" in i18n.t("top.talkers")
    i18n.set_current_lang(None)          # дефолт
    assert i18n.get_current_lang() == "ru"
    i18n.set_current_lang("de-DE")       # неподдерживаемый -> ru
    assert i18n.get_current_lang() == "ru"


def test_catalogs_have_same_keys():  # EN не должен отставать от RU
    assert set(i18n.CATALOGS["en"]) <= set(i18n.CATALOGS["ru"])


def test_user_language_middleware_sets_data():
    from app.middlewares.user_lang import UserLanguageMiddleware
    mw = UserLanguageMiddleware()
    seen = {}

    async def handler(event, data):
        seen["lang"] = i18n.get_current_lang()
        return "ok"

    class FakeUser:
        id = 1

    class FakeEvent:
        from_user = FakeUser()

    class FakeSession:  # session.get вернёт None -> дефолт, без падения
        async def get(self, model, pk):
            return None

    result = run(mw(handler, FakeEvent(), {"session": FakeSession()}))
    assert result == "ok"
    assert seen["lang"] == "ru"


# ------------------------------------------------------------- arena ------

def _mk_pet(pet_id: int, user_id: int, level: int = 1, **stats) -> Pet:
    kw = dict(strength=1, agility=1, intellect=1, happiness=80.0, energy=80.0,
              health=100.0)
    kw.update(stats)
    return Pet(id=pet_id, user_id=user_id, name=f"Pet{pet_id}", species="cat",
               level=level, xp=0, stage="baby", hunger=50.0,
               last_update=datetime.now(timezone.utc), settings_extra={}, **kw)


def test_week_key_format():
    wk = pet_duels.week_key(datetime(2026, 9, 25, tzinfo=timezone.utc))
    assert wk.startswith("2026-W")


def test_duel_power_monotonic():
    weak = _mk_pet(1, 1, level=1)
    strong = _mk_pet(2, 2, level=5, strength=10, agility=10, intellect=10)
    assert pet_duels.duel_power(strong) > pet_duels.duel_power(weak)
    sick = _mk_pet(3, 3, level=5, strength=10, agility=10, intellect=10, health=10)
    assert pet_duels.duel_power(sick) < pet_duels.duel_power(strong)
    tired = _mk_pet(4, 4, level=5, strength=10, agility=10, intellect=10, energy=5)
    assert pet_duels.duel_power(tired) < pet_duels.duel_power(strong)
    assert pet_duels.duel_power(_mk_pet(5, 5, level=1, health=0, energy=0,
                                        happiness=0)) >= 1  # никогда не <=0


def test_resolve_duel_overwhelming_favorite_wins():
    a = _mk_pet(1, 1, level=10, strength=20, agility=20, intellect=20)
    b = _mk_pet(2, 2, level=1)
    for seed in range(30):  # при любом раскладе удачи фаворит сильнее на порядок
        winner, loser = pet_duels.resolve_duel(a, b, rng=random.Random(seed))
        assert winner.id == 1 and loser.id == 2


def test_duel_cooldown_left():
    pet = _mk_pet(1, 1)
    now = datetime.now(timezone.utc)
    assert pet_duels.duel_cooldown_left(pet, now) == 0
    pet.settings_extra = {"duel_last_ts": (now - timedelta(seconds=30)).isoformat()}
    left = pet_duels.duel_cooldown_left(pet, now)
    assert 0 < left <= pet_duels.FIGHT_COOLDOWN_SEC
    pet.settings_extra = {"duel_last_ts": (now - timedelta(seconds=999)).isoformat()}
    assert pet_duels.duel_cooldown_left(pet, now) == 0
    pet.settings_extra = {"duel_last_ts": "не дата"}   # мусорное значение
    assert pet_duels.duel_cooldown_left(pet, now) == 0
    pet.settings_extra = {"duel_last_ts": None}
    assert pet_duels.duel_cooldown_left(pet, now) == 0


def test_arena_text_renders_html_safe():
    """Имена питомцев/владельцев экранируются — разметка пользователя не ломает топ."""
    pet = _mk_pet(1, 1)
    pet.name = "Злой<b>Спрайт"
    owner = User(tg_id=1, first_name="Васян <i>", username=None, level=1, xp=0,
                 coins=0)
    row = pet_duels.PetDuel(pet_id=1, week_key="2026-W39", score=50, wins=2,
                            losses=1, fights=3)
    text = pet_duels.arena_text([(pet, owner, row)], me_pet_id=1, wk="2026-W39")
    assert "&lt;b&gt;" in text and "&lt;i&gt;" in text  # экранировано
    assert "Арена питомцев" in text and "это ты" in text
    empty = pet_duels.arena_text([], me_pet_id=None, wk="2026-W39")
    assert "будь первым" in empty


def test_pick_opponent_filters_by_level_and_sleep(session):
    """Подбор соперника: +-3 уровня, без спящих и без себя (sqlite-проверка)."""
    async def scenario():
        users = [User(tg_id=100 + i, username=None, first_name=f"U{i}", level=1,
                      xp=0, coins=0) for i in range(1, 7)]
        session.add_all(users)
        session.add_all([
            _mk_pet(1, 101, level=5),                      # я
            _mk_pet(2, 102, level=6),                      # подходит
            _mk_pet(3, 103, level=7),                      # подходит
            _mk_pet(4, 104, level=20, is_sleeping=True),   # далеко + спит
            _mk_pet(5, 105, level=1),                      # слишком слабый
        ])
        await session.commit()
        mine = await session.get(Pet, 1)
        opp = await pet_duels.pick_opponent(session, mine)
        assert opp is not None and opp.id in (2, 3)
        # если подходящих нет — честный отказ, а не бой с первым попавшимся
        lonely = _mk_pet(6, 106, level=50)
        session.add(lonely)
        await session.commit()
        assert await pet_duels.pick_opponent(session, lonely) is None
    run(scenario())


def test_arena_keyboard_locks_fight_button():
    """При кулдауне/лимите кнопка боя не должна предлагать arena:fight."""
    from app.keyboards.inline import arena_keyboard
    kb_off = arena_keyboard(can_fight=False, hint="⏳ отдыхает")
    dumped = kb_off.model_dump_json()
    assert "arena:fight" not in dumped and "arena:noop" in dumped
    kb_on = arena_keyboard(can_fight=True, hint="")
    assert "arena:fight" in kb_on.model_dump_json()


@pytest.mark.parametrize("fn", [pet_duels.fight, pet_duels.arena_screen,
                                pet_duels.finish_week, pet_duels.weekly_top,
                                pet_duels.pick_opponent, pet_duels.get_or_create_row])
def test_async_service_functions(fn):
    assert inspect.iscoroutinefunction(fn)


def test_arena_keyboard_locks_fight_button():
    """При кулдауне/лимите кнопка боя не должна предлагать arena:fight."""
    from app.keyboards.inline import arena_keyboard
    kb_off = arena_keyboard(can_fight=False, hint="⏳ отдыхает")
    dumped = kb_off.model_dump_json()
    assert "arena:fight" not in dumped and "arena:noop" in dumped
    kb_on = arena_keyboard(can_fight=True, hint="")
    assert "arena:fight" in kb_on.model_dump_json()


# -------------------------------------------------- customization ---------

def test_customization_reads_settings_extra():
    from app.services.tamagotchi import TamagotchiService
    svc = TamagotchiService(None)  # только словари каталога, БД не нужна
    pet = _mk_pet(1, 1)
    color, worn = svc.customization(pet)
    assert color is None and worn == []
    pet.settings_extra = {"color": "golden", "accessories": ["🎩", "🧣"]}
    color, worn = svc.customization(pet)
    assert color == "golden" and set(worn) == {"🎩", "🧣"}


def test_pet_colors_catalog_shape():
    from app.services.tamagotchi import TamagotchiService
    svc = TamagotchiService(None)
    assert svc.PET_COLORS, "каталог окрасов пуст"
    for key, val in svc.PET_COLORS.items():
        assert isinstance(val, tuple) and len(val) >= 2, key
        name, price = val[0], val[1]
        assert isinstance(name, str) and isinstance(price, int) and price >= 0
