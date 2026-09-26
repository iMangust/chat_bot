"""Реальная погода Петропавловска-Камчатского (Open-Meteo, без API-ключа).

Интеграция «погода → питомец»: текущие условия (код WMO) маппятся в игровые
статы — множители деградации и заметную строку в карточке. Кэш ответа в памяти
на 30 минут; при любом сетевом сбое/таймауте — тихий откат к сезонной модели
(app.utils.formatting.weather_info), чтобы карточка никогда не ломалась из-за
погоды. Время всегда камчатское (app.utils.local_time).
"""
from __future__ import annotations

import time
from datetime import datetime

from loguru import logger

from app.utils.formatting import WEATHER_SEASONS, season_for
from app.utils.local_time import now as local_now

# Петропавловск-Камчатский
LAT, LON = 53.0446, 158.6507
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}"
    "&current=temperature_2m,wind_speed_10m,weather_code"
    "&timezone=Asia%2FKamchatka"
)
CACHE_TTL_SEC = 1800  # 30 минут
HTTP_TIMEOUT_SEC = 3.0

# Коды погоды WMO → (иконка, название, модификаторы деградации за час:
# energy/hunger/happy/hygiene — >1 быстрее падает, <1 медленнее)
WMO_MAP: dict[int, tuple[str, str, dict[str, float]]] = {
    0:  ("☀️", "Ясно", {"happy": 0.8}),                       # счастье падает медленнее
    1:  ("🌤️", "Малооблачно", {}),
    2:  ("⛅", "Переменная облачность", {}),
    3:  ("☁️", "Пасмурно", {"happy": 1.1}),
    45: ("🌫️", "Туман — как дома, туман!", {"energy": 1.2}),
    48: ("🧊", "Изморозь", {"energy": 1.2, "hygiene": 1.1}),
    51: ("🌦️", "Морось", {"happy": 1.1}),
    53: ("🌦️", "Морось", {"happy": 1.1}),
    55: ("🌧️", "Сильная морось", {"happy": 1.15}),
    61: ("🌧️", "Дождь", {"happy": 1.15, "hygiene": 0.9}),
    63: ("🌧️", "Дождь", {"happy": 1.2, "hygiene": 0.9}),
    65: ("🌧️", "Ливень", {"happy": 1.3, "hygiene": 0.85, "energy": 1.1}),
    66: ("🌧️", "Ледяной дождь", {"energy": 1.2}),
    71: ("🌨️", "Снег", {"energy": 1.2, "hunger": 1.15}),
    73: ("❄️", "Снегопад", {"energy": 1.3, "hunger": 1.2}),
    75: ("❄️", "Сильный снегопад", {"energy": 1.4, "hunger": 1.3, "happy": 1.1}),
    77: ("🌨️", "Снежная крупа", {"energy": 1.2}),
    80: ("🌦️", "Кратковременный дождь", {"happy": 1.1}),
    81: ("🌧️", "Дождь с ветром", {"happy": 1.15, "energy": 1.1}),
    82: ("⛈️", "Шквалы ливня", {"happy": 1.3, "energy": 1.2}),
    85: ("🌨️", "Снежные ливни", {"energy": 1.3, "hunger": 1.2}),
    86: ("❄️", "Буря со снегом", {"energy": 1.4, "hunger": 1.3}),
    95: ("⛈️", "Гроза", {"happy": 1.2, "energy": 1.1}),
    96: ("⛈️", "Гроза с градом", {"happy": 1.3, "energy": 1.2}),
    99: ("⛈️", "Гроза с сильным градом", {"happy": 1.3, "energy": 1.2}),
}


def _fallback(dt: datetime | None = None) -> dict:
    """Сезонная модель (офлайн-режим): то же, что показывала карточка раньше."""
    from app.utils.formatting import weather_info
    return weather_info(dt)


def _describe(temp_c: float, wind_kmh: float, code: int) -> str:
    icon, name, _mods = WMO_MAP.get(code, ("🌡️", f"Погода (код {code})", {}))
    note = f"{temp_c:+.0f}°C, ветер {wind_kmh:.0f} км/ч"
    if wind_kmh >= 15:
        note += " — штормит, питомцу лучше сидеть дома 🏠"
    elif temp_c <= -10:
        note += " — трещит мороз, шапка и шарф обязательны 🧣"
    elif temp_c >= 18:
        note += " — камчатское лето! Можно гулять хоть сколько ☀️"
    return f"{icon} {name} ({note})"


async def fetch_real_weather() -> dict | None:
    """Запросить Open-Meteo; None при любой ошибке (вызов не должен бросать)."""
    try:
        import httpx  # ленивый импорт: оффлайн-тесты не требуют сети
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as client:
            resp = await client.get(OPEN_METEO_URL)
            resp.raise_for_status()
            cur = resp.json().get("current", {})
        return {
            "temperature": float(cur["temperature_2m"]),
            "wind": float(cur["wind_speed_10m"]),
            "code": int(cur["weather_code"]),
        }
    except Exception as exc:  # noqa: BLE001 — сеть есть сеть: логируем и живём дальше
        logger.debug("open-meteo unavailable: {}: {}", type(exc).__name__, exc)
        return None


_cache: dict = {"ts": 0.0, "info": None}
_decay_cache: dict = {"ts": 0.0, "mods": {}}


def weather_decay_mods() -> dict[str, float]:
    """Синхронные модификаторы деградации от последней КЭШИРОВАННОЙ реальной погоды.

    apply_decay — горячий синхронный путь; сеть здесь недопустим. Если реальных
    данных ещё нет (или они протухли > TTL) — пустой dict, т.е. только сезонная
    модель. Актуализацию кэша делает kamchatka_weather() при рендере карточки.
    """
    if (time.monotonic() - _decay_cache["ts"]) >= CACHE_TTL_SEC:
        return {}
    return _decay_cache["mods"]


async def kamchatka_weather() -> dict:
    """Погода для карточки питомца: real (кэш 30 мин) либо сезонный фолбэк.

    Возвращает dict вида, совместимый с formatting.weather_info:
    {icon, name, note, decay: {energy,hunger,happy,hygiene}, holiday_*?}
    """
    dt = local_now()
    hol_line = None
    from app.utils.formatting import HOLIDAYS
    hol = HOLIDAYS.get((dt.month, dt.day))
    if hol:
        hol_line = hol

    fresh = (time.monotonic() - _cache["ts"]) < CACHE_TTL_SEC
    if not fresh:
        real = await fetch_real_weather()
        _cache["ts"] = time.monotonic()
        _cache["info"] = real
    real = _cache["info"]

    season_key = season_for(dt)
    base = dict(WEATHER_SEASONS[season_key])
    if real is None:
        info = base
        info["decay"] = {}
        _decay_cache["ts"], _decay_cache["mods"] = 0.0, {}
    else:
        icon, name, mods = WMO_MAP.get(real["code"], (base["icon"], base["name"], {}))
        info = {
            "icon": icon,
            "name": name,
            "note": _describe(real["temperature"], real["wind"], real["code"]),
            "decay": mods,
        }
        _decay_cache["ts"], _decay_cache["mods"] = time.monotonic(), mods
    if hol_line:
        info["holiday_icon"], info["holiday_note"] = hol_line
    return info
