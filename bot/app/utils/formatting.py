"""Формулы баланса XP/уровней и визуальные утилиты (прогресс-бары)."""
from __future__ import annotations

import math

from app.config import get_settings


def xp_needed_for_level(level: int) -> int:
    """XP, необходимый для перехода с `level` на `level+1`.

    Формула: xp_needed = base * level^1.5  (base из конфига, по умолчанию 50).
    Пример (base=50): L1→2: 50, L2→3: 141, L3→4: 260, L5→6: 559, L10→11: 1581.
    """
    base = get_settings().xp_level_base
    return max(int(base * (level ** 1.5)), 1)


def apply_xp(level: int, xp: int, gained: int) -> tuple[int, int, list[int]]:
    """Начисляет XP, возвращает (new_level, new_xp, [список новых уровней]).

    XP «перетекает» между уровнями: избыток сохраняется.
    """
    new_levels: list[int] = []
    xp += gained
    while xp >= xp_needed_for_level(level):
        xp -= xp_needed_for_level(level)
        level += 1
        new_levels.append(level)
    return level, xp, new_levels


def progress_bar(value: float, total: float, length: int = 10) -> str:
    """▰▰▰▰▱▱▱▱▱▱ — прогресс-бар из эмодзи-блоков."""
    if total <= 0:
        total = 1
    filled = min(length, max(0, round(value / total * length)))
    return "▰" * filled + "▱" * (length - filled)


def stat_bar(value: float, length: int = 10) -> str:
    """Прогресс-бар стата питомца (0..100)."""
    return progress_bar(value, 100.0, length)


def format_uptime(seconds: float) -> str:
    """Человекочитаемо: '2 ч 5 мин'."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    return f"{m} мин" if m else "<1 мин"


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return math.floor(min(max(v, lo), hi) * 100) / 100


# ---------------------------------------------------------------------------
# Погода/сезоны: сезонная модификация деградации статов
# ---------------------------------------------------------------------------
WEATHER_SEASONS: dict[str, dict] = {
    "winter": {"icon": "❄️", "name": "Зима", "energy_mult": 1.3, "hunger_mult": 1.2,
               "happy_mult": 1.0, "hygiene_mult": 0.8,
               "note": "зимой питомцы быстрее устают и больше хотят есть"},
    "spring": {"icon": "🌸", "name": "Весна", "energy_mult": 0.9, "hunger_mult": 1.0,
               "happy_mult": 0.8, "hygiene_mult": 1.0,
               "note": "весной настроение поднимается само"},
    "summer": {"icon": "☀️", "name": "Лето", "energy_mult": 1.0, "hunger_mult": 1.1,
               "happy_mult": 0.9, "hygiene_mult": 1.2,
               "note": "летом пачкаемся быстрее, но веселее"},
    "autumn": {"icon": "🍂", "name": "Осень", "energy_mult": 1.1, "hunger_mult": 1.0,
               "happy_mult": 1.15, "hygiene_mult": 1.0,
               "note": "осенняя хандра: счастье тает чуть быстрее"},
}

# праздничные дни (месяц, день) — доп. бонусы в этот день
HOLIDAYS: dict[tuple[int, int], tuple[str, str]] = {
    (1, 1): ("🎄", "С Новым годом! Все награды XP сегодня ×1.5"),
    (2, 14): ("💘", "День святого Валентина: игры приносят +50% счастья"),
    (10, 31): ("🎃", "Хэллоуин: прогулки находят вдвое больше монет"),
    (12, 31): ("🥂", "Канун Нового года: кормления дают +20% сытости"),
}


def season_for(dt) -> str:
    """Метеорологические сезоны северного полушария."""
    m = dt.month
    if m in (12, 1, 2):
        return "winter"
    if m in (3, 4, 5):
        return "spring"
    if m in (6, 7, 8):
        return "summer"
    return "autumn"


# ---------------------------------------------------------------------------
# Праздничные события: модификаторы наград за действия в этот день.
# HOLIDAY_EFFECTS описывает, КАКИЕ механики усиливает праздник; формат —
# dict по «тегам» действий, значения — множители. Тэги читает TamagotchiService
# (xp/hunger/play_happy/walk_coins), поэтому новые праздники добавляются
# без правки кода хендлеров.
# ---------------------------------------------------------------------------
HOLIDAY_EFFECTS: dict[tuple[int, int], dict[str, float]] = {
    (1, 1): {"xp": 1.5},                          # Новый год: все награды XP ×1.5
    (2, 14): {"play_happy": 1.5},                 # Валентин: игры +50% счастья
    (10, 31): {"walk_coins": 2.0},                # Хэллоуин: прогулки ×2 монет
    (12, 31): {"feed_hunger": 1.2},               # Канун НГ: кормления +20% сытости
}


def holiday_effect_mults(dt=None) -> dict[str, float]:
    """Множители на сегодня (пустой dict — обычный день). День считается по камчатскому времени."""
    if dt is None:
        from app.utils.local_time import now as _local_now
        dt = _local_now()
    return HOLIDAY_EFFECTS.get((dt.month, dt.day), {})


def weather_info(dt=None) -> dict:
    """Текущая «погода» для карточки питомца и подсказок (сезон/праздник — по камчатскому времени)."""
    if dt is None:
        from app.utils.local_time import now as _local_now
        dt = _local_now()
    key = season_for(dt)
    info = dict(WEATHER_SEASONS[key])
    hol = HOLIDAYS.get((dt.month, dt.day))
    if hol:
        info["holiday_icon"], info["holiday_note"] = hol
    return info
