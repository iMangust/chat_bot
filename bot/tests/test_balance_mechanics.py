"""Регресс механик баланса: скука, сон, награды игр, глобальные множители.

Без БД: сервис создаётся с session=None, все проверяемые ветки
(play/apply_decay) не обращаются к сессии.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import Pet, PetSpecies
from app.services import balance
from app.services.tamagotchi import TamagotchiService


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pet(**kw) -> Pet:
    p = Pet(name="Тест", species=PetSpecies.cat, user_id=1)
    p.level = kw.get("level", 1)
    p.xp = kw.get("xp", 0)
    p.hunger = kw.get("hunger", 80.0)
    p.happiness = kw.get("happiness", 80.0)
    p.energy = kw.get("energy", 80.0)
    p.hygiene = kw.get("hygiene", 80.0)
    p.health = kw.get("health", 100.0)
    p.is_sleeping = kw.get("is_sleeping", False)
    p.sleep_until = kw.get("sleep_until")
    p.intellect = kw.get("intellect", 1)
    p.settings_extra = kw.get("settings_extra", {}) or {}
    p.last_update = kw.get("last_update", _now())
    return p


# ---------- Скука (boredom) ----------

async def test_boredom_penalty_after_threshold():
    svc = TamagotchiService(None)
    now = _now()
    pet = _pet(last_update=now - timedelta(hours=30),
               settings_extra={"last_care": (now - timedelta(hours=30)).isoformat()})
    before = pet.happiness
    await svc.apply_decay(pet, now)
    assert pet.happiness <= before - balance.get_mult("boredom_penalty") + 2.5
    assert "bored_penalty" in pet.settings_extra


async def test_no_boredom_if_recent_care():
    svc = TamagotchiService(None)
    now = _now()
    pet = _pet(last_update=now - timedelta(hours=30),
               settings_extra={"last_care": (now - timedelta(hours=2)).isoformat()})
    before = pet.happiness
    await svc.apply_decay(pet, now)
    # Только обычный спад счастья (~2ч * 30ч = 60 → кламп 0), но БЕЗ
    # дополнительного штрафа скуки: при 2 часах заботы штраф не ставится.
    assert "bored_penalty" not in pet.settings_extra


async def test_boredom_flag_resets_base_window():
    """После штрафа отсчёт продолжается от момента штрафа — повторный
    штраф возможен только через полный порог, а не сразу."""
    svc = TamagotchiService(None)
    now = _now()
    pet = _pet(last_update=now - timedelta(hours=30),
               settings_extra={"last_care": (now - timedelta(hours=30)).isoformat()})
    await svc.apply_decay(pet, now)
    assert "bored_penalty" in pet.settings_extra
    h_after = pet.happiness
    # Тик через 1 час после штрафа — нового штрафа быть не должно.
    pet.last_update = pet.settings_extra.get("_last_decay", now)
    await svc.apply_decay(pet, now + timedelta(hours=1))
    from datetime import datetime as dt
    penalized_at = dt.fromisoformat(pet.settings_extra["bored_penalty"])
    assert (now + timedelta(hours=1) - penalized_at).total_seconds() < 3600 * 24
    # счастье упало только на обычный спад, не ещё на 15
    assert h_after - pet.happiness < 15


# ---------- Сон: без двойного начисления энергии ----------

async def test_sleep_no_double_energy_regen():
    """Двойного бонуса сна больше нет: до пробуждения по будильнику энергия
    растёт только через sleep_regen/час; сам тик пробуждения не добавляет
    sp['bonus']['sleep_bonus'] сверх времени сна (см. apply_decay)."""
    svc = TamagotchiService(None)
    now = _now()
    pet = _pet(is_sleeping=True, sleep_until=now + timedelta(hours=5),
               energy=20.0, last_update=now - timedelta(hours=2))
    await svc.apply_decay(pet, now)
    assert pet.is_sleeping  # ещё спит — идёт плановая регенерация
    from app.services.tamagotchi import sleep_regen_per_hour
    from app.services.pet_data import SPECIES_DATA
    regen = sleep_regen_per_hour(SPECIES_DATA["cat"])
    expected = 20.0 + 2 * regen
    assert abs(pet.energy - min(100.0, expected)) < max(1.0, regen * 0.35), \
        f"energy={pet.energy}, ожидаем ~{expected} (без двойного бонуса)"


async def test_wake_up_alarm_sets_full_energy_once():
    """Тик пробуждения по будильнику: energy = 100 ровно один раз, без
    накопления сверху при последующих тиках."""
    svc = TamagotchiService(None)
    now = _now()
    pet = _pet(is_sleeping=True, sleep_until=now - timedelta(minutes=1),
               energy=40.0, last_update=now - timedelta(hours=2))
    await svc.apply_decay(pet, now)
    assert not pet.is_sleeping
    assert pet.energy == 100.0


# ---------- Награды за игры ----------

async def test_play_win_beats_lose_gap():
    svc = TamagotchiService(None)
    now = _now()

    async def happy_delta(won):
        pet = _pet(energy=80, hygiene=80, last_update=now,
                   settings_extra={"last_care": now.isoformat(),
                                   "cooldowns": {}})
        before = pet.happiness
        await svc.play(pet, won)
        return pet.happiness - before

    win = await happy_delta(True)
    lose = await happy_delta(False)
    assert win > lose >= 0
    assert win - lose >= 5, "разница победа/поражение должна ощутяться"


async def test_play_respects_balance_keys(monkeypatch):
    monkeypatch.setitem(balance._overrides, "play_win", 30.0)
    monkeypatch.setitem(balance._overrides, "play_lose", 1.0)
    svc = TamagotchiService(None)
    now = _now()
    pet = _pet(energy=80, hygiene=80, happiness=40.0, last_update=now,
               settings_extra={"last_care": now.isoformat(), "cooldowns": {}})
    before = pet.happiness
    await svc.play(pet, True)
    assert pet.happiness - before >= 30 * 0.9 - 1  # базовая награда из override
    assert balance.get_mult("play_win") == 30.0


# ---------- Реестр глобальных множителей ----------

def test_balance_registry_complete():
    for key in ("hunger_decay", "happy_decay", "energy_decay", "sleep_regen",
                "hygiene_decay", "health_decay", "play_win", "play_lose",
                "boredom_penalty", "boredom_hours", "free_actions"):
        assert key in balance.BALANCE_KEYS, key
        env, base, label = balance.BALANCE_KEYS[key]
        assert env.startswith("BALANCE_") and base > 0 and label


def test_balance_override_roundtrip():
    old = balance.get_mult("happy_decay")
    try:
        assert balance.set_mult("happy_decay", 7.5)
        assert balance.get_mult("happy_decay") == 7.5
    finally:
        balance.reset("happy_decay")
    assert balance.get_mult("happy_decay") == old
