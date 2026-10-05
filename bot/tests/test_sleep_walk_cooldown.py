"""Регрессия механик сна и прогулки (apply_decay / _check_cooldown).

Подтверждённые правила баланса:
• во сне счастье и гигиена НЕ падают, энергия растёт со скоростью
  sleep_regen_per_hour() (база + видовой бонус), голод убывает ×0.5;
• штраф «скуки» во сне не срабатывает, а после пробуждения по будильнику
  отсчёт ведётся от времени ЗАСЫПАНИЯ — долгий сон без заботы до него не
  превращается в каскад штрафов сразу после wake;
• время сна НЕ вычитается из кулдаунов действий: иначе «спать между
  играми» было бы эксплойтом против лимита действий;
• на прогулке питомец активен — все декеи идут в полную силу.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Pet
from app.services.tamagotchi import TamagotchiService, sleep_regen_per_hour


def _pet(**kw):
    # Стабильный «сегодняшний» момент для тестов: фиксированная дата в
    # середине лета (июль) — сезонные множители нейтральны, и никакие
    # календарные пороги (скука 24 ч) не зависят от дня запуска набора.
    base = dict(user_id=1, name="Тест", species="cat",
                last_update=datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
                hunger=80, happiness=70, hygiene=70, energy=40,
                health=100, xp=0, level=1)
    base.update(kw)
    return Pet(**base)


@pytest.fixture()
def svc():
    return TamagotchiService(None)


def test_sleep_freezes_happy_and_hygiene(svc):
    now = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
    pet = _pet(is_sleeping=True, sleep_started_at=now - timedelta(hours=6),
               sleep_until=now + timedelta(hours=2),
               hunger=80, happiness=70, hygiene=70, energy=40,
               last_update=now - timedelta(hours=6))
    asyncio.run(svc.apply_decay(pet, now))
    assert pet.happiness == 70   # счастье во сне не падает
    assert pet.hygiene == 70     # гигиена во сне не пачкается
    assert pet.energy > 40       # энергия восстанавливается
    assert pet.is_sleeping       # будильник ещё не сработал


def test_boredom_not_accrued_during_sleep(svc):
    now = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
    care = now - timedelta(hours=30)          # заботы не было 30 часов
    pet = _pet(hunger=80, happiness=70, hygiene=70, energy=80,
               is_sleeping=True,
               sleep_started_at=now - timedelta(hours=5),
               sleep_until=now + timedelta(hours=3),
               last_update=care,
               settings_extra={"last_care": care.isoformat()})
    asyncio.run(svc.apply_decay(pet, now))
    assert pet.happiness == 70                 # штрафа за скуку во сне нет
    assert "bored_penalty" not in (pet.settings_extra or {})


def test_wake_after_long_sleep_no_boredom_cascade(svc):
    """Заснул 26ч назад, будильник был через 8ч сна; тик догоняет сейчас.

    Скука НЕ начисляется за время сна (сон — отдых), поэтому после
    пробуждения питомец не получает штраф −15 сразу: отсчёт «забытья»
    продолжается с момента последнего реального ухода (перед сном).
    """
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)  # июль: сезон нейтрален
    fell_asleep = now - timedelta(hours=26)
    wake_at = fell_asleep + timedelta(hours=8)  # спал 8ч, «проспан» ещё 18ч
    care = fell_asleep - timedelta(minutes=10)  # последняя забота перед сном
    pet = _pet(hunger=90, happiness=70, hygiene=70, energy=10,
               is_sleeping=True,
               sleep_started_at=fell_asleep,
               sleep_until=wake_at,
               last_update=fell_asleep,
               settings_extra={"last_care": care.isoformat()})
    asyncio.run(svc.apply_decay(pet, now))      # тик 1: пробуждение по будильнику
    assert not pet.is_sleeping
    h_after_wake = pet.happiness
    assert h_after_wake >= 34                  # без штрафа −15 (только дрейф бодрствования)
    pet.last_update = now
    asyncio.run(svc.apply_decay(pet, now + timedelta(hours=1)))  # тик 2
    assert pet.happiness >= h_after_wake - 3   # обычный дрейф, без каскада штрафов


def test_sleep_does_not_freeze_game_cooldown(svc):
    """Кулдаун идёт реальным временем и во сне: короткий сон не сбрасывает
    ожидание игры (анти-эксплойт)."""
    now = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
    game_at = now - timedelta(minutes=1)       # играли минуту назад
    pet = _pet(hunger=80, happiness=70, hygiene=70, energy=90,
               is_sleeping=True,
               sleep_started_at=now - timedelta(hours=2),
               sleep_until=now + timedelta(hours=6),
               last_update=now - timedelta(minutes=5),
               settings_extra={"game_at": game_at.isoformat(), "game_uses": 9})
    ok, wait = svc._check_cooldown(pet, "game", 120, now)
    assert not ok and wait > 0                 # сон не «проспал» кулдаун


def test_expired_cooldown_still_passes_regardless_of_sleep(svc):
    """Если реальный elapsed превысил кулдаун — действие доступно,
    даже если питомец всё это время спал."""
    now = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
    pet = _pet(energy=90,
               is_sleeping=True,
               sleep_started_at=now - timedelta(hours=2),
               sleep_until=now + timedelta(hours=6),
               settings_extra={"game_at": (now - timedelta(hours=3)).isoformat(),
                               "game_uses": 9})
    ok, wait = svc._check_cooldown(pet, "game", 120, now)
    assert ok and wait == 0


def test_walk_decays_run_at_full_speed(svc):
    """На прогулке питомец активен: счастье и гигиена падают как обычно."""
    now = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
    pet = _pet(hunger=80, happiness=70, hygiene=70, energy=70,
               walk_until=now + timedelta(hours=1),
               last_update=now - timedelta(hours=2))
    asyncio.run(svc.apply_decay(pet, now))
    assert pet.happiness < 70
    assert pet.hygiene < 70


def test_sleep_regen_single_source(svc):
    """Единая точка правды скорости восстановления ⚡ во сне (вид совёнок).

    Тик короткий (1 час) и питомц не на грани 100⚡ — кламп не искажает
    измерение; глобальный множитель sleep_regen фиксируется, чтобы тест
    не зависел от env/override-настроек окружения прогона.
    """
    from app.services import balance
    from app.services.tamagotchi import _species
    balance.set_mult("sleep_regen", 1.0)
    try:
        now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
        pet = _pet(species="owl", energy=10,
                   is_sleeping=True,
                   sleep_started_at=now - timedelta(hours=1),
                   sleep_until=now + timedelta(hours=6),
                   last_update=now - timedelta(hours=1))
        asyncio.run(svc.apply_decay(pet, now))
        expected = sleep_regen_per_hour(_species(pet))
        assert abs((pet.energy - 10) - expected) < 0.5
    finally:
        balance.reset("sleep_regen")
