"""Интеграционные тесты сервисов на in-memory sqlite.

Проверяем ключевые edge cases:
- антифрод: короткие сообщения, кулдаун, команды не дают XP;
- стрик: инкремент за соседние дни, сброс после пропуска;
- достижения: разблокировка ровно на пороге, награды начислены один раз;
- тамагочи: оффлайн-деградация, лечение, эволюция по уровням.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import Pet, User
from app.services.achievements import AchievementService, seed_achievements
from app.services.activity import ActivityService
from app.services.tamagotchi import TamagotchiService, compute_stage, pet_xp_needed
from app.utils.redis import _mem_store




@pytest.fixture(autouse=True)
def _clear_mem_cooldowns():
    _mem_store.clear()
    yield
    _mem_store.clear()


async def _mk_user(session, tg_id=1, onboarded=True):
    u = User(tg_id=tg_id, first_name="Test", onboarded=onboarded)
    session.add(u)
    await session.flush()
    return u


async def _msg(svc, user_id=1, text="hello world", mid=100, is_command=False):
    return await svc.process_group_message(
        user_id=user_id, chat_id=-100, message_id=mid, text=text,
        has_media=False, media_type=None, is_reply=False,
        mentions_count=0, is_command=is_command,
    )


async def test_short_message_not_counted(session):
    await _mk_user(session)
    svc = ActivityService(session)
    entry = await _msg(svc, text="abc")  # < 5 символов
    assert entry.is_counted is False and entry.skip_reason == "short"
    u = await session.get(User, 1)
    assert u.xp == 0 and u.coins == 0


async def test_command_not_counted(session):
    await _mk_user(session)
    svc = ActivityService(session)
    entry = await _msg(svc, text="/help", is_command=True)
    assert entry.skip_reason == "command"


async def test_cooldown_blocks_second_message(session):
    await _mk_user(session)
    svc = ActivityService(session)
    e1 = await _msg(svc, mid=1)
    e2 = await _msg(svc, mid=2)  # сразу второе — кулдаун
    assert e1.is_counted and not e2.is_counted and e2.skip_reason == "cooldown"
    u = await session.get(User, 1)
    assert u.xp == 2 and u.coins == 1  # только за первое


async def test_unregistered_user_ignored(session):
    svc = ActivityService(session)
    assert await _msg(svc, user_id=999) is None


async def test_streak_increment_and_reset(session):
    u = await _mk_user(session)
    svc = ActivityService(session)
    now = datetime.now(timezone.utc)
    u.last_active_date = now - timedelta(days=1)
    await _msg(svc, mid=1)
    assert u.streak_days == 2
    # пропуск дня: последняя активность 3 дня назад -> серия сбрасывается в 1
    u.last_active_date = now - timedelta(days=3)
    _mem_store.clear()  # сбрасываем кулдаун между фазами теста
    await _msg(svc, mid=2)
    assert u.streak_days == 1


async def test_achievement_unlock_once_with_rewards(session):
    await seed_achievements(session)
    u = await _mk_user(session)
    svc = ActivityService(session)
    ach_svc = AchievementService(session)
    for i in range(1, 11):
        _mem_store.clear()  # обходим кулдаун намеренно в тесте (заменяем Redis)
        res = await _msg(svc, mid=10 + i)
        assert res.is_counted
    # msg_10 должен быть разблокирован (10 засчитанных сообщений)
    rows = await ach_svc.list_for_user(u.tg_id)
    m10 = next((a, ur) for a, ur in rows if a.code == "msg_10")
    assert m10[1].unlocked_at is not None
    xp_after = u.xp
    # повторная проверка не выдаёт награду второй раз
    again = await ach_svc.check(u.tg_id, {"messages_total": 10})
    assert all(a.code != "msg_10" for a in again)
    await session.refresh(u)
    assert u.xp == xp_after


async def test_hidden_achievement_requires_force(session):
    await seed_achievements(session)
    u = await _mk_user(session)
    ach_svc = AchievementService(session)
    got = await ach_svc.check(u.tg_id, {"messages_total": 1})
    assert all(not a.is_hidden for a in got)
    night = await ach_svc.unlock_by_code(u.tg_id, "night_owl")
    assert night is not None and night.code == "night_owl"
    # повторно — None
    assert await ach_svc.unlock_by_code(u.tg_id, "night_owl") is None


async def test_pet_decay_offline(session, monkeypatch):
    # отключаем сезонную модификацию (осенний множитель голода +15%),
    # чтобы проверить базовые скорости деградации
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "weather_enabled", False, raising=False)
    u = await _mk_user(session)
    pet = Pet(user_id=u.tg_id, name="Тест", hunger=80, happiness=80,
              energy=80, hygiene=80, health=100,
              last_update=datetime.now(timezone.utc) - timedelta(hours=5))
    session.add(pet)
    await session.flush()
    svc = TamagotchiService(session)
    await svc.apply_decay(pet)
    # 5 часов: hunger 80-20=60, happiness 70, energy 70, hygiene 65
    assert 59 <= pet.hunger <= 61
    assert 69 <= pet.happiness <= 71
    assert 64 <= pet.hygiene <= 66
    assert pet.health == 100  # уход ещё хороший


async def test_pet_decay_season_multiplier_applied(session, monkeypatch):
    """Осенью счастье падает на 15% быстрее базовой скорости (сезонная механика)."""
    from app.config import Settings, get_settings
    monkeypatch.setattr(get_settings(), "weather_enabled", True, raising=False)
    assert get_settings().weather_enabled  # убедимся, что override применился
    u = await _mk_user(session)
    autumn = datetime(2026, 10, 15, tzinfo=timezone.utc)
    pet = Pet(user_id=u.tg_id, name="Сезонный", hunger=80, happiness=80,
              energy=80, hygiene=80, health=100,
              last_update=autumn - timedelta(hours=5))
    session.add(pet)
    await session.flush()
    svc = TamagotchiService(session)
    await svc.apply_decay(pet, now=autumn)
    # без сезона: 80-2*5=70; с осенним множителем happy x1.15: 80-11.5=68.5
    assert pet.happiness < 70
    assert abs(pet.happiness - 68.5) <= 0.5


async def test_pet_becomes_sick_when_neglected(session):
    u = await _mk_user(session)
    pet = Pet(user_id=u.tg_id, name="Грязнуля", hunger=10, hygiene=10, health=60,
              last_update=datetime.now(timezone.utc) - timedelta(hours=10))
    session.add(pet)
    await session.flush()
    svc = TamagotchiService(session)
    await svc.apply_decay(pet)
    assert pet.health < 50 and pet.sick_since is not None


async def test_pet_feed_cooldown(session):
    u = await _mk_user(session)
    pet = Pet(user_id=u.tg_id, name="Кот")
    session.add(pet)
    await session.flush()
    svc = TamagotchiService(session)
    r1 = await svc.feed(pet, {"hunger": 20})
    r2 = await svc.feed(pet, {"hunger": 20})
    assert "Ням" in r1 and "Подожди" in r2


async def test_pet_evolution_stage_changes(session):
    u = await _mk_user(session)
    pet = Pet(user_id=u.tg_id, name="Дракоша", level=1, xp=0)
    session.add(pet)
    await session.flush()
    svc = TamagotchiService(session)
    # считаем XP строго по уровням питомца: суммарно нужно для L1->L4
    need = sum(pet_xp_needed(l) for l in (1, 2, 3))
    levels = await svc.add_pet_xp(pet, need)
    assert pet.level == 4 and levels == [2, 3, 4]
    assert pet.stage == compute_stage(pet.level)


# ---------------------------------------------------------------------------
# v1.4.5: прогулка не должна «самосъедаться» в apply_decay
# ---------------------------------------------------------------------------

async def test_walk_flag_survives_apply_decay(session):
    """apply_decay отмечает завершение, но НЕ снимает walk_until — иначе
    фоновый тик scheduler'а терял награду прогулки до прихода пользователя."""
    from datetime import datetime, timedelta, timezone as tz
    u = await _mk_user(session)
    now = datetime.now(tz.utc)
    pet = Pet(user_id=u.tg_id, name="Гуляка",
              walk_until=now - timedelta(minutes=1),
              last_update=now - timedelta(hours=1))
    session.add(pet)
    await session.flush()
    svc = TamagotchiService(session)
    changed = await svc.apply_decay(pet, now=now)
    assert changed is True                 # факт завершения зафиксирован
    assert pet.walk_until is not None      # НО флаг жив — ждёт хендлера


async def test_walk_collect_gated_by_time(session):
    """_collect_walk_result отдаёт награду только по истечении срока прогулки."""
    from datetime import datetime, timedelta, timezone as tz
    from app.handlers.tamagotchi import _collect_walk_result
    u = await _mk_user(session)
    now = datetime.now(tz.utc)
    pet = Pet(user_id=u.tg_id, name="Ранняя пташка",
              walk_until=now + timedelta(hours=1))   # ещё гуляет
    session.add(pet)
    await session.flush()
    svc = TamagotchiService(session)
    assert _collect_walk_result(svc, pet, session) is None
    pet.walk_until = now - timedelta(minutes=5)      # срок вышел
    res = _collect_walk_result(svc, pet, session)
    assert res is not None and len(res) == 3
    text, coins, xp = res
    assert isinstance(text, str) and coins >= 0 and xp > 0


def test_no_shop_inventory_stubs_in_tamagotchi():
    """Заглушки «Магазин откроется на Этапе 4» удалены: shop.router регистрируется
    после tamagotchi.router и эти хендлеры перехватывали pet:shop/pet:inv первыми,
    из-за чего настоящий магазин был недостижим из хаба питомца."""
    import ast
    src = open("app/handlers/tamagotchi.py", encoding="utf-8").read()
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
    assert "act_shop_stub" not in names and "act_inv_stub" not in names
    assert "Этапе 4" not in src
