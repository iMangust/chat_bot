"""Глобальные множители баланса питомца (админ-настройки).

Позволяет крутить скорость падения/роста статов и щедрость наград БЕЗ
правки кода — двумя способами:

  1. Переменные окружения в bot/.env (значения по умолчанию при старте):
       BALANCE_HUNGER_DECAY=4.0        # ч/час, база (× видовые/сезонные моды)
       BALANCE_HAPPY_DECAY=2.0
       BALANCE_ENERGY_DECAY=1.2
       BALANCE_SLEEP_REGEN=8.0         # прирост ⚡ во сне, ч/час
       BALANCE_HYGIENE_DECAY=3.0
       BALANCE_HEALTH_DECAY=3.0        # здоровье при полном забвении
       BALANCE_PLAY_WIN=12.0           # 😊 за победу в игре (до множителей)
       BALANCE_PLAY_LOSE=5.0           # 😊 за поражение («проиграть не страшно»)
       BALANCE_BOREDOM_PENALTY=15      # штраф 😊 за скуку (>24 ч без заботы)
       BALANCE_FREE_ACTION_USES=3      # бесплатных действий до платных

  2. На лету — из экранных настроек бота (только ADMIN_IDS): кнопки
     «⚙️ Баланс» → «set:bal:<ключ>» инкрементно меняют множитель ×1.25
     (шаг назад ÷1.25), значения живут в памяти процесса и дублируются в
     .env-совместимый вид. После рестарта возвращаются к env.

Все потребители (tamagotchi.apply_decay/play/wake, scheduler) читают
значения через get_mult() — единая точка правды.
"""
from __future__ import annotations

from loguru import logger

# Ключ → (имя переменной окружения, базовое значение, подпись для экрана)
BALANCE_KEYS: dict[str, tuple[str, float, str]] = {
    "hunger_decay":   ("BALANCE_HUNGER_DECAY",   4.0,  "🍎 Сытость падает, ч/час"),
    "happy_decay":    ("BALANCE_HAPPY_DECAY",    2.0,  "😊 Счастье падает, ч/час"),
    "energy_decay":   ("BALANCE_ENERGY_DECAY",   1.2,  "⚡ Энергия падает, ч/час"),
    "sleep_regen":    ("BALANCE_SLEEP_REGEN",    8.0,  "⚡ Прирост во сне, ч/час"),
    "hygiene_decay":  ("BALANCE_HYGIENE_DECAY",  3.0,  "🫧 Гигиена падает, ч/час"),
    "health_decay":   ("BALANCE_HEALTH_DECAY",   3.0,  "❤️ Здоровье падает (забвение), ч/час"),
    "play_win":       ("BALANCE_PLAY_WIN",       12.0, "🎾 😊 за победу в игре"),
    "play_lose":      ("BALANCE_PLAY_LOSE",      5.0,  "🎾 😊 за поражение"),
    "boredom_penalty":("BALANCE_BOREDOM_PENALTY", 15.0,"😿 Штраф 😊 за скуку"),
    "boredom_hours":  ("BALANCE_BOREDOM_HOURS",  24.0, "⏱ Часов без заботы до штрафа"),
    "free_actions":   ("BALANCE_FREE_ACTION_USES", 3.0,"🆓 Бесплатных действий"),
}

_MIN, _MAX = 0.0, 1000.0
_STEP = 1.25          # размер шага «+» / «−» (умножение/деление)

_overrides: dict[str, float] = {}


def _env_default(key: str) -> float:
    env_name, base, _ = BALANCE_KEYS[key]
    try:
        from app.config import get_settings
        raw = getattr(get_settings(), env_name.lower(), None)
        if raw is None:
            import os
            raw = os.environ.get(env_name)
        return float(raw) if raw is not None else base
    except Exception as exc:
        logger.debug("balance env default {} failed: {}", key, type(exc).__name__)
        return base


def get_mult(key: str) -> float:
    """Текущее значение множителя баланса (override → env → база)."""
    if key in _overrides:
        return _overrides[key]
    return _env_default(key)


def set_mult(key: str, value: float) -> bool:
    if key not in BALANCE_KEYS:
        return False
    v = max(_MIN, min(_MAX, float(value)))
    _overrides[key] = round(v, 4)
    return True


def bump(key: str, up: bool = True) -> float | None:
    """Шаг настройки из UI: ×1.25 (up) или ÷1.25. Возвращает новое значение."""
    if key not in BALANCE_KEYS:
        return None
    cur = get_mult(key)
    nxt = cur * _STEP if up else cur / _STEP
    set_mult(key, nxt)
    return _overrides[key]


def reset(key: str | None = None) -> None:
    if key is None:
        _overrides.clear()
    else:
        _overrides.pop(key, None)


def env_overridden_keys() -> set[str]:
    """Ключи, у которых значение задано через .env (не базовое)."""
    return {k for k in BALANCE_KEYS if _env_default(k) != BALANCE_KEYS[k][1]}


def snapshot() -> dict[str, float]:
    return {k: get_mult(k) for k in BALANCE_KEYS}
