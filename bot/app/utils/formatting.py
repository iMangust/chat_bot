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
