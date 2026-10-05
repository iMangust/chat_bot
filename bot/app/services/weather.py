from __future__ import annotations

import asyncio
import os
import threading
import time as _mono
from datetime import datetime, timedelta, timezone

from loguru import logger

from app.utils.formatting import WEATHER_SEASONS, season_for
from app.utils.html_text import esc
from app.utils.local_time import now as local_now, user_tz

def _weather_geo() -> tuple[str, float, float]:
    """Город и координаты берутся из Settings (переменные WEATHER_* в .env),
    с фолбэком на прямое чтение env — чтобы панель могла менять их без рестарта."""
    try:
        from app.config import get_settings
        st = get_settings()
        return (st.weather_city or "Петропавловск-Камчатский",
                float(st.weather_lat), float(st.weather_lon))
    except Exception:
        return (os.getenv("WEATHER_CITY", "Петропавловск-Камчатский"),
                float(os.getenv("WEATHER_LAT", "53.0446")),
                float(os.getenv("WEATHER_LON", "158.6507")))

LAT: float = 0.0
LON: float = 0.0
WEATHER_CITY: str = ""

def refresh_geo() -> None:
    """Перечитать WEATHER_* из .env/настроек (вызывается панелью после сохранения)."""
    global WEATHER_CITY, LAT, LON
    WEATHER_CITY, LAT, LON = _weather_geo()

refresh_geo()
CACHE_TTL_SEC = 1800
WEATHER_REAL_ENABLED = os.getenv("WEATHER_REAL_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
RETRY_AFTER_SEC = 1200
REFRESH_INTERVAL_SEC = 3 * 3600

FORCE_FALLBACK = os.getenv("WEATHER_FORCE_FALLBACK", "").strip().lower() in {"1", "true", "yes", "on"}

WEATHER_SOURCE = "openweather"
OWM_BASE = "https://api.openweathermap.org/data"
# One Call 3.0 убран: на бесплатном тарифе он недоступен (401), а /2.5-эндпоинты
# (weather + forecast) дают те же данные без отдельного подписочного тарифа.
OWM_CURRENT_URL = f"{OWM_BASE}/2.5/weather"
OWM_FORECAST_URL = f"{OWM_BASE}/2.5/forecast"
OPENWEATHER_KEY_ENV = "OPENWEATHER_API_KEY"
_HTTP_CONNECT_TIMEOUT = 6.0
_HTTP_READ_TIMEOUT = 10.0

def openweather_key() -> str:
    for var in (OPENWEATHER_KEY_ENV, "OPENWEATHER_API_TOKEN",
                "OWM_API_KEY", "OWM_APP_ID"):
        tok = os.getenv(var, "").strip()
        if tok:
            return tok
    try:
        from app.config import get_settings
        s = get_settings()
        for attr in (OPENWEATHER_KEY_ENV.lower(), "openweather_api_token",
                     "openweather_app_id"):
            tok = str(getattr(s, attr, "") or "").strip()
            if tok:
                return tok
        extra = getattr(s, "model_extra", None) or {}
        for key in (OPENWEATHER_KEY_ENV.lower(), "openweather_api_token",
                    "owm_api_key", "owm_app_id"):
            tok = str(extra.get(key, "") or "").strip()
            if tok:
                return tok
    except Exception:
        pass
    return ""

def weather_source_line() -> str:
    """Служебная строка о состоянии источника погоды.

    В пользовательских экранах погоды не показывается (см. _weather_text);
    оставлена для диагностики/логики, опционально может выводиться в панели.
    """
    if openweather_key():
        src = "OpenWeather (openweathermap.org)"
    else:
        src = ("OpenWeather (openweathermap.org) — ключ ещё не задан, "
               "сейчас показывается сезонная модель")
    return f"🔗 Источник данных: {src}."

def _timeout():
    import httpx
    return httpx.Timeout(connect=_HTTP_CONNECT_TIMEOUT, read=_HTTP_READ_TIMEOUT,
                         write=8.0, pool=5.0)

def _transport():
    import httpx
    return httpx.AsyncHTTPTransport(retries=1, trust_env=True,
                                    local_address="0.0.0.0")

async def fetch_real_weather() -> dict | None:
    import sys as _sys
    mod = _sys.modules[__name__]
    if not openweather_key() or getattr(mod, "FORCE_FALLBACK", False):
        return None
    try:
        owm = await getattr(mod, "fetch_openweather")()
    except Exception as exc:
        logger.warning("openweather unavailable: {}: {}", type(exc).__name__, str(exc)[:200])
        owm = None
    if owm is None:
        logger.info("погода: OpenWeather недоступен — показываем кэш/сезонную модель")
    return owm

def _owm_to_wmo(owm_id: int, temp_c: float | None = None) -> int:
    try:
        i = int(owm_id)
    except (TypeError, ValueError):
        return 2
    group, sub = i // 100, i % 100
    if group == 2:
        return 96 if sub >= 20 else 95
    if group == 3:
        return {1: 51, 2: 53, 3: 55}.get(sub, 53) or 51
    if group == 5:
        if sub == 3:
            return 82
        if sub == 2:
            return 63
        if sub == 1:
            return 61
        return 60 if (temp_c is not None and temp_c < 0) else 51
    if group == 6:
        if sub in (1, 2, 3, 4):
            base = {1: 71, 2: 73, 3: 75, 4: 77}[sub]
        elif sub in (5, 6):
            base = 71
        else:
            base = 85 if sub == 7 else 86
        if temp_c is not None and temp_c > 1.0 and base in (73, 75):
            base = 71
        return base
    if group == 7:
        if sub == 50:
            return 45
        if sub == 70:
            return 77
        return 45
    if group == 8:
        if sub == 0:
            return 0
        if sub <= 2:
            return 1 if sub == 1 else 2
        if sub == 3:
            return 2
        return 3
    return 2

CLOUDY_BY_PCT = ((11.0, 0), (30.0, 1), (69.0, 2), (100.1, 3))

def _cloud_code_by_pct(cloud_pct: float | None) -> int | None:
    if cloud_pct is None:
        return None
    try:
        p = float(cloud_pct)
    except (TypeError, ValueError):
        return None
    for hi, code in CLOUDY_BY_PCT:
        if p < hi:
            return code
    return 3

def _parse_owm_current(js: dict) -> dict | None:
    main = js.get("main") or {}
    t = main.get("temp")
    if t is None:
        return None
    w0 = (js.get("weather") or [{}])[0]
    code = _owm_to_wmo(w0.get("id"), float(t))
    wind_ms = (js.get("wind") or {}).get("speed")
    hum = main.get("humidity")
    feels = main.get("feels_like", t)
    cloud = js.get("clouds") or {}
    cloud_pct = cloud.get("all")
    if code in SUNNY_CODES | {3}:
        by_pct = _cloud_code_by_pct(cloud_pct)
        if by_pct is not None:
            code = by_pct
    dt = js.get("dt")
    sysd = js.get("sunrise"), js.get("sunset")
    is_day = True
    try:
        if isinstance(dt, (int, float)):
            sr, ss = sysd
            if isinstance(sr, (int, float)) and isinstance(ss, (int, float)):
                is_day = sr <= dt < ss
    except (TypeError, ValueError):
        pass
    rain = (js.get("rain") or {})
    precip = float(rain.get("1h") or rain.get("3h") or 0.0)
    snow = (js.get("snow") or {})
    snow_mm = float(snow.get("1h") or snow.get("3h") or 0.0)
    desc = str(w0.get("description") or "")
    return {
        "temperature": float(t),
        "wind": round(float(wind_ms or 0) * 3.6, 1),
        "gust": 0.0,
        "code": int(code),
        "feels": float(feels if feels is not None else t),
        "humidity": float(hum or 0),
        "is_day": bool(is_day),
        "precip": precip or snow_mm,
        "snow_cm": round(snow_mm * 7.0, 1) if (
            float(t) <= 1.0 and (code in SNOW_CODES or code in (66, 71, 77, 85, 86))) else 0.0,
        "cloud": float(cloud_pct if cloud_pct is not None else 60),
        "pressure_hpa": float((main.get("pressure") or 0)),
        "description": desc,
        "source": "openweather",
        "obs": False,
        "hourly": {},
    }

def _owm_utc_hour(ts: int) -> str:
    from datetime import datetime as _dt, timezone as _tz
    try:
        return _dt.fromtimestamp(int(ts), tz=_tz.utc).strftime("%Y-%m-%dT%H")
    except (ValueError, OSError, OverflowError):
        return ""

def _parse_owm_forecast_hours(js: dict) -> list[dict]:
    out: list[dict] = []
    try:
        for f in js.get("list") or []:
            w0 = (f.get("weather") or [{}])[0]
            main = f.get("main") or {}
            t = main.get("temp")
            code = _owm_to_wmo(w0.get("id"), t)
            fcloud = (f.get("clouds") or {}).get("all")
            if code in SUNNY_CODES | {3}:
                by_pct = _cloud_code_by_pct(fcloud)
                if by_pct is not None:
                    code = by_pct
            elif w0.get("id") == 800 and code <= 1:
                by_pct = _cloud_code_by_pct(fcloud)
                if by_pct is not None:
                    code = max(code, by_pct)
            rain = float(((f.get("rain") or {}).get("3h")) or 0.0)
            snow = float(((f.get("snow") or {}).get("3h")) or 0.0)
            out.append({
                "time": str(f.get("dt_txt") or "")[:13].replace(" ", "T"),
                "temp": float(t or 0.0),
                "code": int(code),
                "precip": rain or snow,
                "gust": round(float(((f.get("wind") or {}).get("gust")) or 0.0) * 3.6, 1),
                "cloud": fcloud,
            })
    except (AttributeError, TypeError, ValueError):
        return []
    return out

async def fetch_openweather() -> dict | None:
    import httpx
    key = openweather_key()
    if not key:
        return None
    params = {"lat": LAT, "lon": LON, "units": "metric", "lang": "ru",
              "appid": key}
    try:
        async with httpx.AsyncClient(timeout=_timeout(), http2=False,
                                     transport=_transport(),
                                     follow_redirects=True) as client:
            # Только /2.5-эндпоинты (One Call 3.0 убран — недоступен на
            # бесплатном тарифе и только тормозил каждый тик лишним запросом).
            resp = await client.get(OWM_CURRENT_URL, params=params)
            resp.raise_for_status()
            snap = _parse_owm_current(resp.json())
            if snap is None:
                logger.warning("openweather: пустой/непонятный ответ /2.5/weather")
                return None
            try:
                rf = await client.get(OWM_FORECAST_URL, params=params)
                if rf.status_code == 200:
                    hours = _parse_owm_forecast_hours(rf.json())
                    if hours:
                        snap["hourly"] = {
                            "time": [h["time"] for h in hours],
                            "temp": [h["temp"] for h in hours],
                            "code": [h["code"] for h in hours],
                            "precip": [h["precip"] for h in hours],
                            "gust": [h["gust"] for h in hours],
                        }
                else:
                    logger.warning("openweather forecast: HTTP {}",
                                   rf.status_code)
            except Exception as exc:
                logger.debug("openweather forecast unavailable: {}",
                             str(exc)[:120])
            # Если /2.5/forecast не отдал часовых точек, а снимок пришёл из
            # /2.5/weather (там hourly пуст по определению), недельный экран
            # остался бы без данных до следующего тика. Достаём точки из
            # кэша прошлых запросов — это честнее сезонной заглушки, т.к.
            # данные всё равно живые (просто чуть старше).
            if not ((snap.get("hourly") or {}).get("time")):
                cached_pts = _hours_cache.get("points") or []
                if cached_pts and (_mono.monotonic() - _hours_cache["ts"]) \
                        <= max(_ttl(), 6 * 3600):
                    snap["hourly"] = {
                        "time": [p.get("time") for p in cached_pts],
                        "temp": [p.get("temp") for p in cached_pts],
                        "code": [p.get("code") for p in cached_pts],
                        "precip": [p.get("precip") for p in cached_pts],
                        "gust": [p.get("gust") for p in cached_pts],
                        "cloud": [p.get("cloud") for p in cached_pts],
                    }
            return snap
    except Exception as exc:
        es = str(exc)
        hint = ""
        if "getaddrinfo" in es or "Errno 11001" in es:
            hint = " (DNS не резолвит api.openweathermap.org)"
        elif "ConnectTimeout" in type(exc).__name__:
            hint = " (TCP/443 к api.openweathermap.org висит — файрвол/провайдер)"
        logger.warning("openweather unavailable: {}: {}{}", type(exc).__name__,
                       es[:200], hint)
        return None

def _ttl() -> float:
    if not WEATHER_REAL_ENABLED:
        return -1.0
    try:
        from app.config import get_settings
        s = get_settings()
        if getattr(s, "weather_real_enabled", True) is False:
            return -1.0
        hours = getattr(s, "weather_refresh_hours", None)
        if hours is not None and float(hours) > 0:
            return max(60.0, float(hours) * 3600)
        return max(60.0, float(getattr(s, "weather_cache_minutes", 180)) * 60)
    except Exception:
        return float(REFRESH_INTERVAL_SEC)


async def fetch_forecast_hours() -> list[dict]:
    """Почасовые точки /2.5/forecast (до ~4 дней, шаг 3 ч) независимо от
    основного снимка. Нужны, когда основной источник — Open-Meteo или
    снимок /2.5 без часовых данных: иначе недельный экран нечем заполнять."""
    key = openweather_key()
    if not key or FORCE_FALLBACK or not WEATHER_REAL_ENABLED:
        return []
    import httpx
    try:
        async with httpx.AsyncClient(timeout=_timeout(), transport=_transport(),
                                     follow_redirects=True) as client:
            rf = await client.get(OWM_FORECAST_URL, params={
                "lat": LAT, "lon": LON, "units": "metric", "lang": "ru",
                "appid": key})
            if rf.status_code != 200:
                logger.warning("openweather forecast(standalone): HTTP {}",
                               rf.status_code)
                return []
            return _parse_owm_forecast_hours(rf.json())
    except Exception as exc:
        logger.debug("openweather forecast(standalone) unavailable: {}",
                     str(exc)[:120])
        return []

async def background_refresh() -> dict | None:
    try:
        return await _ensure_fresh(force=True)
    except Exception as exc:
        logger.warning("weather: фоновое обновление не удалось: {}", exc)
        return None

WMO_MAP: dict[int, tuple[str, str, dict[str, float]]] = {
    0:  ("☀️", "Ясно", {"happy": 0.8}),
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
    from app.utils.formatting import weather_info
    return weather_info(dt)

def _describe(temp_c: float, wind_kmh: float, code: int, feels: float | None = None,
              gust: float | None = None, night: bool = False) -> tuple[str, str, str]:
    icon, name, _mods = WMO_MAP.get(code, ("🌡️", f"Погода (код {code})", {}))
    if night and code in SUNNY_CODES:
        icon, name = "🌙", "Ясная ночь"
    wtype = classify_weather(code, temp_c, wind_kmh, gust=gust)
    if wtype in ("sunny", "cloudy", "overcast"):
        icon, name = sky_label(code)
        if night and code <= 1:
            icon, name = "🌙", "Ясная ночь" if code == 0 else "Ясно с луной"
    else:
        icon, name = _EFF_LABELS.get(wtype, (icon, name))
    note = f"{temp_c:+.0f}°C"
    if feels is not None and abs(feels - temp_c) >= 3.0:
        note += f" (ощущается {feels:+.0f}°)"
    wind_ms = max(0.0, float(wind_kmh or 0.0)) / 3.6
    gust_ms = max(0.0, float(gust or 0.0)) / 3.6 if gust else 0.0
    note += (f", ветер {wind_ms:.1f} м/с"
             + (f" (порывы до {gust_ms:.1f})" if gust_ms >= wind_ms * 1.5 and gust_ms > 0 else ""))
    if wtype == "stormy":
        note += " — штормит, питомцу лучше сидеть дома 🏠"
    elif wtype == "frosty" and temp_c > COLD_TEMP_C:
        note += " — ветер опасный, как шторм: питомцу лучше сидеть дома 🏠"
    elif wtype == "frosty":
        pass
    elif temp_c <= -10:
        note += " — трещит мороз, шапка и шарф обязательны 🧣"
    elif temp_c >= 18:
        note += " — камчатское лето! Можно гулять хоть сколько ☀️"
    return icon, name, note

_cache: dict = {"ts": 0.0, "info": None, "next_try_mono": 0.0}
_decay_cache: dict = {"ts": 0.0, "mods": {}}
# Сырые почасовые точки последнего успешного запроса прогноза
# ([{"time": "YYYY-MM-DDTHH", "temp", "code", "precip", "gust", "cloud"}]).
# Нужны для недельного экрана погоды: /2.5/forecast отдаёт ~4 дн.
# до 5 дней (3 ч шаг). Кэш живёт столько же, сколько основной кэш погоды.
_hours_cache: dict = {"ts": 0.0, "points": []}

def _reset_state_for_tests() -> None:
    global _fetch_inflight, _inflight_force
    _cache.update({"ts": 0.0, "info": None, "next_try_mono": 0.0})
    _decay_cache.update({"ts": 0.0, "mods": {}})
    _hours_cache.update({"ts": 0.0, "points": []})
    with _FETCH_GUARD:
        _fetch_inflight = None
        _inflight_force = False

async def _do_fetch() -> dict | None:
    import sys as _sys
    try:
        real = await getattr(
            _sys.modules[__name__], "fetch_real_weather")()
    except Exception as exc:
        real = None
        logger.warning("погода: fetch исключение: {}", exc)
    if isinstance(real, dict) and not real:
        real = None
    ts = _mono.monotonic()
    _cache["ts"] = ts
    _cache["info"] = real
    # сохраняем почасовые точки для недельного экрана (wthr:week)
    hp = ((real or {}).get("hourly") or {}).get("time") or []
    if hp:
        hd = real.get("hourly") or {}
        _hours_cache["ts"] = ts
        _hours_cache["points"] = [
            {"time": t, "temp": (hd.get("temp") or [None] * len(hp))[i],
             "code": (hd.get("code") or [None] * len(hp))[i],
             "precip": (hd.get("precip") or [0.0] * len(hp))[i],
             "gust": (hd.get("gust") or [0.0] * len(hp))[i],
             "cloud": (hd.get("cloud") or [None] * len(hp))[i]}
            for i, t in enumerate(hp)]
    elif real is not None:
        # Успешный снимок БЕЗ почасовых точек (/2.5/weather не содержит
        # hourly, а /2.5/forecast в этот раз упал). Раньше эта ветка принудительно
        # затирала _hours_cache — и недельный экран откатывался к сезонной
        # оценке («+0…+6°» все дни), хотя живые данные были. Теперь старые
        # часовые точки сохраняются до истечения их собственного TTL
        # (проверка в hourly_points); затираем их только при полном
        # недоступии источника (real is None) — см. else ниже.
        pass
    else:
        _hours_cache["ts"], _hours_cache["points"] = 0.0, []
    if real is None:
        _cache["next_try_mono"] = ts + RETRY_AFTER_SEC
        logger.warning("погода: источник недоступен — показываем последний "
                       "кэш/сезонную модель (следующая попытка через {} c)",
                       RETRY_AFTER_SEC)
    else:
        _cache["next_try_mono"] = 0.0
    return real

# «Окно шторма»: негативный кэш ошибки сети — после неудачной попытки не
# долбим API до истечения next_try_mono. Модульная функция: используется и
# _ensure_fresh, и kamchatka_weather (см. там же — никогда не ждать сеть
# синхронно в callback-хендлере).
def _storm_window_active(now_m: float | None = None) -> bool:
    if now_m is None:
        now_m = _mono.monotonic()
    return now_m < _cache.get("next_try_mono", 0.0)


async def _ensure_fresh(force: bool = False) -> dict | None:
    global _fetch_inflight

    def _fresh_enough(now_m: float) -> bool:
        ttl = _ttl()
        if ttl <= 0:
            return False
        # Кэш с УСПЕШНЫМИ живыми данными живёт полный TTL и всегда
        # приоритетнее любого «шторм-окна» (негативного кэша ошибки).
        # Раньше проверка next_try_mono стояла здесь же и после серии
        # неудачных запросов (например, спам кнопкой «Обновить» до её
        # удаления) затирала даже успешный fetch на 20 минут — недельный
        # экран откатывался к сезонной модели. Теперь негативное окно
        # учитывается только когда свежих успешных данных НЕТ.
        if _cache.get("info") is not None and \
                (now_m < _cache["ts"] or (now_m - _cache["ts"]) < ttl):
            return True
        # Успешных данных нет (или они протухли): шторм-окно блокирует
        # повторные сетевые попытки; при его наличии считаем кэш
        # «достаточно свежим», чтобы не долбить API — наружу уйдёт
        # сезонная модель.
        if _storm_window_active(now_m):
            return True
        return False

    def _start_or_join(now_m: float):
        global _fetch_inflight, _inflight_force
        loop = asyncio.get_running_loop()
        with _FETCH_GUARD:
            fut = _fetch_inflight
            if fut is not None and not fut.done() and fut.get_loop() is loop:
                return fut, False, False
            if not force and _storm_window_active(now_m):
                if not (fut is not None and not fut.done() and _inflight_force):
                    return None, False, True
            fut = loop.create_future()
            _fetch_inflight = fut
            _inflight_force = force
            return fut, True, False

    fut: "asyncio.Future | None" = None
    try:
        now_m = _mono.monotonic()
        if not force and _fresh_enough(now_m):
            return _cache.get("info")

        fut, owner, storm_hit = _start_or_join(now_m)
        if storm_hit:
            return None
        if not owner:
            return await asyncio.shield(fut)

        real = await _do_fetch()
        fut.set_result(real)
        return real
    finally:
        with _FETCH_GUARD:
            inflight = _fetch_inflight
        if inflight is not None and fut is not None and inflight is fut:
            global _inflight_force
            with _FETCH_GUARD:
                _fetch_inflight = None
                _inflight_force = False

_FETCH_GUARD = threading.Lock()
_fetch_inflight: "asyncio.Future | None" = None
_inflight_force = False

SUNNY_CODES = {0, 1, 2}

_EFF_LABELS = {
    "sunny": ("☀️", "Солнечно"),
    "cloudy": ("⛅", "Переменная облачность"),
    "overcast": ("☁️", "Пасмурно"),
    "foggy": ("🌫️", "Туман"),
    "rainy": ("🌧️", "Дождь"),
    "snowy": ("❄️", "Снег"),
    "stormy": ("⛈️", "Гроза"),
    "frosty": ("🥶", "Мороз / шквальный ветер"),
}
RAIN_CODES = {51, 53, 55, 61, 63, 65, 66, 80, 81, 82}
CLOUDY_LABEL = {0: "Ясно", 1: "Малооблачно", 2: "Переменная облачность",
                3: "Пасмурно"}
STORM_CODES = {95, 96, 99, 85, 86}
SNOW_CODES = {71, 73, 75, 77}
FOG_CODES = {45, 48}
COLD_TEMP_C = -7.0
WIND_STORM_KMH = 39.0
WIND_GALE_KMH = 25.0
GUST_ESCALATION_FACTOR = 2.0

def classify_weather(code: int, temp_c: float, wind_kmh: float, *,
                     gust: float | None = None, snow_cm: float = 0.0) -> str:
    if code < 0:
        return ""
    extreme_wind = (wind_kmh >= WIND_STORM_KMH
                    or (gust or 0.0) >= WIND_STORM_KMH
                    and (gust or 0.0) >= wind_kmh * GUST_ESCALATION_FACTOR)
    if temp_c <= COLD_TEMP_C or extreme_wind:
        return "frosty"
    if code in STORM_CODES:
        return "stormy"
    if code in SNOW_CODES or (snow_cm > 0 and temp_c <= 2):
        return "snowy"
    if code in RAIN_CODES:
        return "rainy"
    if code in FOG_CODES:
        return "foggy"
    if code == 3:
        return "overcast"
    if code in (1, 2):
        return "cloudy"
    if code == 0:
        return "sunny"
    return ""

def sky_label(code: int) -> tuple[str, str]:
    icon = {0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️"}.get(int(code), "⛅")
    return icon, CLOUDY_LABEL.get(int(code), "Переменная облачность")

WALK_MODS: dict[str, dict] = {
    "sunny":  {"mult": 1.30, "happy_add": 8, "energy_add": 5, "stat_add": 0.15,
               "sick_pct": 0.00,
               "tip": "☀️ Идеальная погода для прогулки: бонус к находкам, "
                      "😊 Счастью и ⚡ Энергии! Есть шанс подрасти в силе/ловкости."},
    "cloudy": {"mult": 1.10, "happy_add": 4, "energy_add": 2, "stat_add": 0.05,
               "sick_pct": 0.02,
               "tip": "⛅ Комфортная погода — отличная прогулка."},
    "overcast": {"mult": 1.00, "happy_add": 1, "energy_add": 0, "stat_add": 0.0,
                 "sick_pct": 0.03,
                 "tip": "☁️ Пасмурно, но сухо — спокойная прогулка."},
    "foggy":  {"mult": 1.00, "happy_add": 2, "energy_add": 0, "stat_add": 0.0,
               "sick_pct": 0.05,
               "tip": "🌫️ Туман... гулять можно, но питомец будет вялым."},
    "rainy":  {"mult": 0.90, "happy_add": 0, "energy_add": -3, "stat_add": 0.0,
               "sick_pct": 0.15,
               "tip": "🌧️ Дождь: меньше находок, есть шанс промокнуть! 🤒"},
    "snowy":  {"mult": 1.05, "happy_add": 3, "energy_add": -2, "stat_add": 0.05,
               "sick_pct": 0.12,
               "tip": "❄️ Снег — весело (снежки закаляют!), но холодно: риск простуды."},
    "stormy": {"mult": 0.70, "happy_add": -3, "energy_add": -5, "stat_add": 0.0,
               "sick_pct": 0.25,
               "tip": "⛈️ Гроза/буран! Лучше отложить прогулку — опасно."},
    "frosty": {"mult": 0.80, "happy_add": -2, "energy_add": -4, "stat_add": 0.0,
               "sick_pct": 0.25,
               "tip": "🥶 Мороз/шквальный ветер: береги лапы, сиди дома 🏠"},
}

def _cur_mods(real: dict | None) -> tuple[str, dict]:
    if not real:
        return "", {}
    wtype = classify_weather(int(real.get("code", -1)), float(real.get("temperature", 0)),
                             float(real.get("wind", 0)), gust=float(real.get("gust", 0) or 0),
                             snow_cm=float(real.get("snow_cm", 0) or 0))
    return wtype, WALK_MODS.get(wtype, {})

def walk_mods() -> dict:
    t_ = weather_effect().get("type", "")
    return WALK_MODS.get(t_, {})

def _walk_window_hours(real: dict | None, hours: int = 3) -> list[dict]:
    if not real:
        return []
    h = real.get("hourly") or {}
    times = h.get("time") or []
    if not times:
        return []
    try:
        now_iso = local_now().strftime("%Y-%m-%dT%H")
    except Exception:
        return []
    out = []
    for i, ts in enumerate(times):
        if str(ts)[:13] < now_iso[:13]:
            continue
        try:
            out.append({
                "temp": float(h["temp"][i]),
                "code": int(h["code"][i]),
                "precip": float(h["precip"][i] or 0),
                "gust": float((h.get("gust") or [0] * len(times))[i] or 0),
            })
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if len(out) >= max(1, hours):
            break
    return out

def forecast_walk_mods(hours: int = 3) -> dict:
    real = _cache.get("info")
    cur_type, cur = _cur_mods(real)
    parts = _walk_window_hours(real, hours)
    if not parts or not cur:
        return {**cur, "type": cur_type, "parts": []}
    n = len(parts)
    agg = {"mult": 0.0, "happy_add": 0.0, "energy_add": 0.0,
           "stat_add": 0.0, "sick_pct": 0.0}
    types = []
    for p in parts:
        t_, m_ = _cur_mods({"code": p["code"], "temperature": p["temp"],
                            "wind": 0.0, "gust": p["gust"],
                            "snow_cm": 1.0 if (p["precip"] > 0 and p["temp"] <= 2) else 0.0})
        types.append(t_)
        if not m_:
            m_ = cur
        for k in agg:
            agg[k] += float(m_.get(k, 0)) / n
    order = ["sunny", "cloudy", "overcast", "snowy", "foggy", "rainy", "frosty", "stormy"]
    best = min(types, key=lambda t: order.index(t) if t in order else 99)
    worst = max(types, key=lambda t: order.index(t) if t in order else 99)
    return {**agg, "type": cur_type, "best": best, "worst": worst,
            "parts": types}

async def refresh_weather() -> dict | None:
    return await _ensure_fresh(force=True)

async def walk_forecast_line() -> str:
    real = _cache.get("info")
    if not real or (_mono.monotonic() - _cache["ts"]) >= REFRESH_INTERVAL_SEC:
        return ""
    fw = forecast_walk_mods(3)
    tip = (WALK_MODS.get(fw.get("type") or "") or {}).get("tip", "")
    line = f"🌦️ {tip}" if tip else ""
    parts = fw.get("parts") or []
    if parts:
        icons = {"sunny": "☀️", "cloudy": "⛅", "overcast": "☁️", "foggy": "🌫️", "rainy": "🌧️",
                 "snowy": "❄️", "stormy": "⛈️", "frosty": "🥶", "": "❔"}
        seq = " → ".join(icons.get(t, "❔") for t in parts)
        line += f"\n🕐 Следующие {len(parts)} ч: {seq}"
        if fw.get("worst") in ("stormy", "frosty") and fw.get("type") not in ("stormy", "frosty"):
            line += "\n⚠️ Портитcя — лучше вернуться пораньше!"
        elif fw.get("best") == "sunny" and fw.get("type") != "sunny":
            line += "\n💡 Прояснится — можно задержаться на солнышке ☀️"
    return line

def weather_effect() -> dict:
    real = _cache.get("info")
    if not real:
        return {}
    wtype, _mods = _cur_mods(real)
    eff_map = {
        "sunny":  {"happy_delta": 5, "energy_delta": 3, "sick_chance": 0.0,
                   "label": "☀️ Солнечно — питомец радуется жизни!"},
        "cloudy":  {"happy_delta": 1, "energy_delta": 1, "sick_chance": 0.0,
                    "label": "⛅ Переменная облачность — спокойный день"},
        "overcast": {"happy_delta": -3, "energy_delta": 0, "sick_chance": 0.0,
                     "label": "☁️ Пасмурно — лёгкая хандра"},
        "foggy":  {"happy_delta": -2, "energy_delta": -3, "sick_chance": 0.03,
                   "label": "🌫️ Туман — клонит в сон…"},
        "rainy":  {"happy_delta": -4, "energy_delta": -2, "hygiene_delta": -3,
                   "sick_chance": 0.08, "label": "🌧️ Дождь — грустим и мёрзнем…"},
        "snowy":  {"happy_delta": -3, "energy_delta": -3, "hunger_delta": 3,
                   "sick_chance": 0.10, "label": "❄️ Снег — тянет в сон и есть хочется"},
        "stormy": {"happy_delta": -6, "energy_delta": -4, "sick_chance": 0.15,
                   "label": "⛈️ Штормит — питомцу лучше сидеть дома 🏠"},
        "frosty": {"happy_delta": -4, "energy_delta": -3, "sick_chance": 0.20,
                   "label": "🥶 Мороз/шквальный ветер — бережем лапы!"},
    }
    base = eff_map.get(wtype)
    if base is None:
        return {}
    eff = dict(base)
    eff["type"] = wtype
    if not real.get("is_day", True):
        eff["energy_delta"] = eff.get("energy_delta", 0) - 2
        eff["happy_delta"] = eff.get("happy_delta", 0) - 2
        eff["night"] = True
        eff["label"] = "🌙 Ночь на улице — " + eff["label"].split("—", 1)[-1].strip().lower()
    if float(real.get("humidity", 0) or 0) >= 90 and wtype in ("rainy", "foggy", "snowy"):
        eff["sick_chance"] = min(0.35, eff.get("sick_chance", 0.0) + 0.05)
    if wtype in ("frosty", "stormy"):
        eff["extreme"] = True
    return eff

def _signed(v: float) -> str:
    return f"+{v:g}" if v > 0 else f"{v:g}"

PET_STATS = {
    "hunger":  ("🍎", "Сытость",  "насколько питомец накормлен"),
    "happy":   ("😊", "Счастье",  "насколько питомцу весело"),
    "energy":  ("⚡", "Энергия",  "сколько сил осталось на игры и прогулки"),
    "hygiene": ("🫧", "Гигиена",  "насколько питомец чист"),
    "health":  ("❤️", "Здоровье", "как себя чувствует — при ≤50 питомец болен"),
}

def stat_name(stat_key: str) -> str:
    icon, name, _ = PET_STATS[stat_key]
    return f"{icon} {name}"

STAT_MEANING = {
    f"{k}_delta": (stat_name(k), meaning)
    for k, (_, _, meaning) in {
        "happy": PET_STATS["happy"], "energy": PET_STATS["energy"],
        "hunger": PET_STATS["hunger"], "hygiene": PET_STATS["hygiene"],
    }.items()
}

def tick_line(eff: dict) -> str:
    parts = []
    for key in ("happy_delta", "energy_delta", "hunger_delta", "hygiene_delta"):
        d = eff.get(key)
        if not d:
            continue
        parts.append(f"{STAT_MEANING[key][0]} {_signed(d)}")
    if not parts:
        return ""
    return "↳ тик погоды: " + ", ".join(parts)

def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    a = abs(int(n))
    if a % 100 in range(11, 15):
        return many
    last = a % 10
    if last == 1:
        return one
    if last in (2, 3, 4):
        return few
    return many

_WEATHER_TICKS_PER_DAY = 8

_TICK_MAG_LABELS = ((7, "ощутимо"), (3, "заметно"), (0, "слабо"))

def _tick_mag_label(mag: int) -> str:
    for threshold, label in _TICK_MAG_LABELS:
        if mag >= threshold:
            return label
    return "слабо"

STAT_LEGEND = (
    "🍎 Сытость — еда · 😊 Счастье — веселье · ⚡ Энергия — силы · "
    "🫧 Гигиена — чистота · ❤️ Здоровье — болезнь при ≤50. "
    "На языке бота «💭 Настроение» карточки — общая оценка состояния, "
    "а не отдельный стат."
)

_STAT_LINE = STAT_LEGEND

def stat_deltas_explanation(eff: dict) -> list[str]:
    return []

def weather_window_end(pet, dt: datetime | None = None) -> datetime | None:
    extra = getattr(pet, "settings_extra", None) or {}
    last_iso = extra.get("weather_applied_at")
    if not last_iso:
        return None
    try:
        started = datetime.fromisoformat(str(last_iso))
    except ValueError:
        return None
    if started.tzinfo is None:
        from app.utils.local_time import localize
        started = localize(started)
    return started + timedelta(hours=3)

def _hhmm(dt: datetime | None) -> str:
    if dt is None:
        return "?"
    return f"{dt:%H:%M}"

def _minutes_text(left_min: float) -> str:
    m = int(max(0.0, left_min))
    if m <= 0:
        return "меньше минуты"
    m = round(m / 5.0) * 5
    if m == 0:
        return "меньше минуты"
    return f"{m} мин"

def _next_tick_info(pet, dt=None):
    end = weather_window_end(pet, dt) if pet is not None else None
    now_dt = dt or local_now()
    if end is None:
        return None, None
    if end <= now_dt:
        return now_dt, 0.0
    return end, (end - now_dt).total_seconds() / 60.0

def _tick_time_parts(pet, dt=None):
    end, left_min = _next_tick_info(pet, dt)
    if end is not None and left_min is not None:
        if left_min <= 0:
            return "текущее окно уже истекло — следующий тик возможен уже сейчас"
        return f"{_hhmm(end)} (через {_minutes_text(left_min)})"
    return "скоро"

def _effects_window_stamp(pet=None, window_end=None) -> str:
    if isinstance(window_end, str):
        return window_end or "скоро"
    if pet is not None:
        return _tick_time_parts(pet)
    if isinstance(window_end, tuple):
        end, left_min = window_end
        if left_min is not None and left_min <= 0:
            return "уже сейчас (через 0 мин)"
        return f"{_hhmm(end)} (через {_minutes_text(left_min or 0)})"
    if isinstance(window_end, datetime):
        now_dt = local_now()
        end = window_end
        if end.tzinfo is None:
            from app.utils.local_time import localize
            end = localize(end)
        if end <= now_dt:
            return "уже сейчас (через 0 мин)"
        return f"{_hhmm(end)} (через {_minutes_text((end - now_dt).total_seconds() / 60.0)})"
    return "скоро"

def next_weather_tick(pet, dt: datetime | None = None) -> datetime:
    end = weather_window_end(pet) if pet is not None else None
    now_dt = dt or local_now()
    if end is None or end <= now_dt:
        return now_dt
    return end

def weather_effects_lines(eff: dict | None = None, *, walk: bool = False,
                          forecast: dict | None = None,
                          window_end: "datetime | tuple | str | None" = None,
                          pet=None, show_legend: bool = False) -> list[str]:
    eff = eff if eff is not None else weather_effect()
    if not eff:
        return []
    wtype = eff.get("type", "")
    lines: list[str] = []

    tick = tick_line(eff)
    extra_bits = []
    sick = eff.get("sick_chance", 0.0)
    if sick:
        extra_bits.append(f"риск простуды {round(sick * 100)}%")
    if eff.get("extreme"):
        extra_bits.append("статы на улице деградируют быстрее")
    if tick:
        line = f"   {tick}"
        line += " (уже учтено в статах)"
        if extra_bits:
            line += " · " + " · ".join(extra_bits)
        lines.append(line)
    elif extra_bits:
        lines.append("   ↳ " + " · ".join(extra_bits))
    if show_legend and tick:
        lines.append(f"   • {_STAT_LINE}")
    if tick:
        try:
            stamp = _effects_window_stamp(pet, window_end)
            lines.append(f"   ⏳ Следующий тик погоды — {stamp}.")
        except Exception:
            pass

    if walk:
        wm = WALK_MODS.get(wtype) or {}
        src = forecast if forecast else None
        wp = []
        mult = float((src or wm).get("mult", 1.0))
        if abs(mult - 1.0) >= 0.05:
            verdict = "прогулка полезна 📈" if mult > 1 else "прогулка почти бессмысленна 📉"
            wp.append(f"находки/XP ×{mult:.2f} — {verdict}")
        ha = float((src or wm).get("happy_add", 0))
        ea = float((src or wm).get("energy_add", 0))
        if ha:
            wp.append(f"{stat_name('happy')} {_signed(ha)} после возвращения")
        if ea:
            wp.append(f"{stat_name('energy')} {_signed(ea)} после возвращения")
        sa = float((src or wm).get("stat_add", 0))
        if sa:
            wp.append(f"шанс подрасти 💪/🏃 {round(sa * 100)}%")
        ws = float((src or wm).get("sick_pct", 0))
        if ws:
            wp.append(f"риск промокнуть {round(ws * 100)}%")
        if wp:
            lines.append("   🚶 прогулка: " + ", ".join(wp))
        pf = (forecast or {}).get("parts") or []
        if pf:
            icons = {"sunny": "☀️", "cloudy": "⛅", "overcast": "☁️", "foggy": "🌫️", "rainy": "🌧️",
                     "snowy": "❄️", "stormy": "⛈️", "frosty": "🥶", "": "❔"}
            seq = " → ".join(icons.get(t, "❔") for t in pf)
            pl = f"   🕐 следующие {len(pf)} ч: {seq}"
            worst, best = (forecast or {}).get("worst"), (forecast or {}).get("best")
            if worst in ("stormy", "frosty") and wtype not in ("stormy", "frosty"):
                pl += " ⚠️ портится — вернись пораньше!"
            elif best == "sunny" and wtype != "sunny":
                pl += " 💡 прояснится — задержись на солнышке ☀️"
            lines.append(pl)
    return lines

def weather_hint_block(*, walk: bool = False, pet=None,
                       show_legend: bool = False) -> str:
    eff = weather_effect()
    if not eff:
        return ""
    fc = forecast_walk_mods(3) if walk else None
    stamp = _tick_time_parts(pet) if pet is not None else "скоро"
    lines = [f"🌦️ {eff.get('label', '')}"] + weather_effects_lines(
        eff, walk=walk, forecast=fc, window_end=stamp, show_legend=show_legend)
    return "\n".join(lines)

async def weather_hint_block_fresh(*, walk: bool = False, pet=None,
                                   show_legend: bool = False) -> str:
    if _cache.get("info") is not None and (_mono.monotonic() - _cache["ts"]) < _ttl():
        return weather_hint_block(walk=walk, pet=pet,
                                  show_legend=show_legend)
    try:
        await asyncio.wait_for(_ensure_fresh(), timeout=4.0)
    except (asyncio.TimeoutError, Exception):
        pass
    return weather_hint_block(walk=walk, pet=pet, show_legend=show_legend)

def apply_weather_to_pet(pet, dt=None) -> str | None:
    import random as _random
    from app.utils.local_time import now as _local_now
    eff = weather_effect()
    if not eff:
        return None
    dt = dt or _local_now()
    extra = dict(getattr(pet, "settings_extra", None) or {})
    last_iso = extra.get("weather_applied_at")
    if last_iso:
        try:
            if (dt - datetime.fromisoformat(last_iso)).total_seconds() < 3 * 3600:
                return None
        except ValueError:
            pass
    parts = []
    hd = eff.get("happy_delta", 0)
    ed = eff.get("energy_delta", 0)
    gd = eff.get("hunger_delta", 0)
    yd = eff.get("hygiene_delta", 0)
    if hd:
        pet.happiness = max(0, min(100, pet.happiness + hd))
        parts.append(f"{stat_name('happy')} {_signed(hd)}")
    if ed:
        pet.energy = max(0, min(100, pet.energy + ed))
        parts.append(f"{stat_name('energy')} {_signed(ed)}")
    if gd:
        pet.hunger = max(0, min(100, pet.hunger + gd))
        parts.append(f"{stat_name('hunger')} {_signed(gd)}")
    if yd:
        pet.hygiene = max(0, min(100, pet.hygiene + yd))
        parts.append(f"{stat_name('hygiene')} {_signed(yd)}")
    sick_line = ""
    chance = eff.get("sick_chance", 0.0)
    try:
        from app.services.tamagotchi import TamagotchiService
        g = TamagotchiService(None).gear_bonuses(pet)
        chance = max(0.0, chance * (1.0 + g.get("sick_chance_pct", 0.0)))
    except Exception:
        pass
    if chance and getattr(pet, "sick_since", None) is None and _random.random() < chance:
        pet.sick_since = dt
        pet.health = max(5, pet.health - 10)
        sick_line = "\n🤒 Питомец промок на улице и ПРОСТУДИЛ! Нужно лечение 💊"
    extra["weather_applied_at"] = dt.isoformat()
    pet.settings_extra = extra
    if not parts and not sick_line:
        return None
    line = f"{eff['label']} ({', '.join(parts)})" if parts else eff["label"]
    window_note = ""
    if parts:
        _end, _left_min = _next_tick_info(pet, dt)
        if _end is not None and _left_min is not None:
            window_note = (f"\n⏳ Следующий тик погоды — {_hhmm(_end)} "
                           f"(через {_minutes_text(_left_min)}).")
        else:
            window_note = "\n⏳ Следующий тик погоды — скоро."
    return line + sick_line + window_note

def weather_window_line(pet, dt=None) -> str:
    end, left_min = _next_tick_info(pet, dt)
    if end is not None and left_min is not None:
        if left_min <= 0:
            return ("⏳ Текущее окно погоды уже истекло — следующий тик "
                    "возможен уже сейчас.")
        return (f"⏳ Следующий тик погоды — {_hhmm(end)} "
                f"(действует ещё {_minutes_text(left_min)}).")
    return "⏳ Следующий тик погоды — скоро."

def weather_decay_mods() -> dict[str, float]:
    if (_mono.monotonic() - _decay_cache["ts"]) >= _ttl():
        return {}
    return _decay_cache["mods"]

async def kamchatka_weather() -> dict:
    dt = local_now()
    hol_line = None
    from app.utils.formatting import HOLIDAYS
    hol = HOLIDAYS.get((dt.month, dt.day))
    if hol:
        hol_line = hol

    # ⚠️ КРИТИЧНО: никогда не ходим в сеть синхронно. Сетевой fetch живёт
    # до connect(6)+read(10) сек на источник (несколько источников — дольше).
    # Раньше при протухшем кэше этот вызов ждал _ensure_fresh() прямо внутри
    # callback-хендлера → aiogram не успевал ответить на тап за 10 секунд
    # Telegram'а («кнопка неактивна», «игра началась, но первый же шаг
    # зависает»: все экраны с render_async упирались в погоду). Наружу всегда
    # отдаём последний кэш или сезонную модель; обновление кэша выполняется
    # фоново (см. tasks/scheduler.py), а storm-window сам ограничивает
    # частоту сетевых попыток.
    fresh = (_mono.monotonic() - _cache["ts"]) < _ttl()
    if not fresh and _cache.get("info") is None and not _storm_window_active():
        try:
            await asyncio.wait_for(_ensure_fresh(), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass
    real = _cache["info"]

    season_key = season_for(dt)
    base = dict(WEATHER_SEASONS[season_key])
    if real is None:
        info = base
        info["decay"] = {}
        _decay_cache["ts"], _decay_cache["mods"] = 0.0, {}
    else:
        eff_icon, eff_name, eff_note = _describe(
            real["temperature"], real["wind"], real["code"],
            feels=real.get("feels"), gust=real.get("gust"),
            night=not real.get("is_day", True))
        info = {
            "icon": eff_icon,
            "name": eff_name,
            "note": eff_note,
            "decay": mods if (mods := WMO_MAP.get(real["code"], ("", "", {}))[2]) else {},
        }
        eff = weather_effect()
        if eff.get("type") in ("stormy", "frosty") and real["code"] not in STORM_CODES:
            info["decay"] = {**info["decay"], "energy": info["decay"].get("energy", 1.0) * 1.15,
                             "happy": info["decay"].get("happy", 1.0) * 1.1}
        _decay_cache["ts"], _decay_cache["mods"] = _mono.monotonic(), info["decay"]
    if hol_line:
        info["holiday_icon"], info["holiday_note"] = hol_line
    return info


# ============================================================================
# Погодный раздел главного меню (wthr:*)
# ============================================================================

_WEEKDAY_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_WEEKDAY_FULL_RU = ("понедельник", "вторник", "среда", "четверг",
                    "пятница", "суббота", "воскресенье")
_MONTHS_RU = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")
# Ключевые слова classify_weather → «человеческое» описание со своего экрана
_TYPE_WORD_RU = {"clear": "ясно", "sunny": "ясно", "cloudy": "переменная облачность",
                 "overcast": "облачно", "rainy": "дождь", "drizzle": "морось",
                 "thunderstorm": "гроза", "snowy": "снег", "sleet": "мокрый снег",
                 "foggy": "туман", "windy": "ветрено", "stormy": "шторм"}
_DAY_ICON = {0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️", 45: "🌫️", 48: "🧊",
             51: "🌦️", 53: "🌦️", 55: "🌧️", 61: "🌧️", 63: "🌧️", 65: "🌧️",
             66: "🌧️", 71: "🌨️", 73: "❄️", 75: "❄️", 77: "🌨️", 80: "🌦️",
             81: "🌧️", 82: "⛈️", 85: "🌨️", 86: "❄️", 95: "⛈️", 96: "⛈️",
             99: "⛈️"}
# «Хорошая погода для прогулки» — типы из classify_weather с множителем ≥ 1.0
_GOOD_TYPES = {"sunny", "cloudy", "overcast", "snowy"}


def _fmt_temp(t) -> str:
    try:
        return f"{float(t):+.0f}°"
    except (TypeError, ValueError):
        return "··°"


def _day_head(d, today_: date) -> str:
    """Заголовок дня недели: «Сегодня», «Завтра» или полное название.

    Никаких сокращений «пн/вт» — человек читает «Понедельник», а не календарь
    из терминала.
    """
    if d == today_:
        return "Сегодня"
    if d == today_ + timedelta(days=1):
        return "Завтра"
    return _WEEKDAY_FULL_RU[d.weekday()].capitalize()


def _date_label(d: date) -> str:
    return f"{d.day} {_MONTHS_RU[d.month - 1]}"


def _type_word(wtype: str) -> str:
    return _TYPE_WORD_RU.get((wtype or "").strip().lower(), "")


def _parse_hour_key(s: str) -> datetime | None:
    """'YYYY-MM-DDTHH' (UTC) -> aware-datetime UTC."""
    from datetime import timezone as _tz
    try:
        return datetime.strptime(str(s)[:13], "%Y-%m-%dT%H").replace(tzinfo=_tz.utc)
    except (TypeError, ValueError):
        return None


def _season_temp_c(dt: datetime) -> float:
    """Сезонная оценка температуры (°C) для фолбэк-режима — чтобы кнопка меню
    и экран не были пустыми, когда живые данные недоступны. Это не прогноз,
    поэтому вывод помечается как ориентировочный."""
    drift = ((dt.timetuple().tm_yday - 15) / 365.0) * 14.0 - 7.0
    return round(drift)


async def weather_now() -> dict:
    """Снимок текущей погоды для виджетов/экрана: live-данные или сезонная модель."""
    real = await _ensure_fresh()
    dt = local_now()
    if isinstance(real, dict) and real.get("temperature") is not None:
        icon, name, note = _describe(
            real["temperature"], real.get("wind", 0.0), int(real.get("code", 2)),
            feels=real.get("feels"), gust=real.get("gust"),
            night=not real.get("is_day", True))
        wtype = classify_weather(int(real.get("code", 2)), real["temperature"],
                                 real.get("wind", 0.0), gust=real.get("gust"),
                                 snow_cm=real.get("snow_cm", 0.0))
        return {"live": True, "icon": icon, "name": name, "note": note,
                "temp": float(real["temperature"]), "code": int(real.get("code", 2)),
                "type": wtype, "wind_ms": round(float(real.get("wind", 0.0)) / 3.6, 1),
                "humidity": real.get("humidity"), "pressure": real.get("pressure_hpa")}
    base = _fallback(dt)
    # temp — сезонная ОЦЕНКА (не прогноз): чтобы кнопка меню и экран
    # «Сегодня» показывали градусы даже без живых данных OpenWeather.
    return {"live": False, "icon": base.get("icon", "🌡️"),
            "name": base.get("name", "—"), "note": base.get("note", ""),
            "temp": _season_temp_c(dt), "est": True, "code": -1, "type": "",
            "wind_ms": None, "humidity": None, "pressure": None}


async def weather_button_label() -> str:
    """Подпись кнопки погоды в главном меню: «🌦 +3° Дождь» (не длиннее ~26 симв.)."""
    try:
        w = await weather_now()
    except Exception as exc:  # noqa: BLE001 — меню не должно падать из-за погоды
        logger.debug("weather_button_label: {}", exc)
        return "🌦️ Погода"
    if not w["live"]:
        # Кнопка всё равно «живая»: показываем хотя бы сезонную оценку
        # с градусами, а не просто слово «Погода» (пользователь просил
        # текущую погоду прямо на кнопке).
        season_label = f"{w['icon']} {_fmt_temp(w.get('temp'))} {w['name']}"
        if len(season_label) > 26:
            season_label = season_label[:26].rstrip()
        _label_cache["text"], _label_cache["ts"] = season_label, _mono.monotonic()
        return season_label
    label = f"{w['icon']} {_fmt_temp(w['temp'])} {w['name']}"
    if len(label) > 26:
        label = label[:26].rstrip()
    _label_cache["text"], _label_cache["ts"] = label, _mono.monotonic()
    return label


_label_cache: dict = {"text": "", "ts": 0.0}


def cached_weather_button_label(max_age_sec: float = REFRESH_INTERVAL_SEC) -> str:
    """Метка кнопки погоды из кэша (без сети). '' — если свежих данных нет."""
    if (_label_cache["text"]
            and (_mono.monotonic() - _label_cache["ts"]) < max_age_sec):
        return _label_cache["text"]
    return ""


async def hourly_points() -> list[dict]:
    """Почасовые точки прогноза (UTC-ключи) из кэша последнего запроса.

    Если часового кэша нет вовсе (ts==0 — например, процесс перезапустили,
    а первый успешный снимок пришёл без hourly на бесплатном тарифе),
    принудительно обновляем данные один раз и собираем точки заново.
    Это закрывает случай «неделя пуста до первого серверного тика».
    """
    ttl = _ttl()
    fresh = _hours_cache["ts"] > 0 and \
        (_mono.monotonic() - _hours_cache["ts"]) < ttl
    if not fresh:
        await _ensure_fresh()
    if not _hours_cache["points"] and ttl > 0:
        # Часовых точек так и нет (снимок приходит из источника без hourly).
        # Достаём прогноз /2.5/forecast отдельным запросом (раз в TTL, не
        # чаще) — иначе недельный экран навсегда остаётся сезонной заглушкой.
        try:
            hours = await fetch_forecast_hours()
        except Exception as exc:  # noqa: BLE001 — экран не должен падать
            logger.debug("hourly_points: standalone forecast failed: {}", exc)
            hours = []
        if hours:
            ts = _mono.monotonic()
            _hours_cache["ts"] = ts
            _hours_cache["points"] = hours
    pts = [_hours_cache["points"] and p for p in _hours_cache["points"]]
    return [p for p in pts if p]


def _day_rows(points: list[dict], days: int = 7) -> list[dict]:
    """Группировка почасовых точек по камчатским суткам: min/max/самая частая «погода»."""
    by_day: dict = {}
    for p in points:
        dt = _parse_hour_key(p.get("time") or "")
        if dt is None:
            continue
        d = dt.astimezone(user_tz()).date()
        by_day.setdefault(d, []).append(p)
    today_ = local_now().date()
    rows: list[dict] = []
    for i in range(days):
        d = today_ + timedelta(days=i)
        ps = by_day.get(d)
        if not ps:
            continue
        temps = [float(p["temp"]) for p in ps if p.get("temp") is not None]
        codes = [int(p["code"]) for p in ps if p.get("code") is not None]
        precip = max((float(p.get("precip") or 0.0) for p in ps), default=0.0)
        gust = max((float(p.get("gust") or 0.0) for p in ps), default=0.0)
        wind = max((float(p.get("wind") or 0.0) for p in ps), default=0.0)
        code = max(set(codes), key=codes.count) if codes else 2
        tmin, tmax = (min(temps), max(temps)) if temps else (None, None)
        temp_for_cls = ((tmin + tmax) / 2.0) if temps else 0.0
        wtype = classify_weather(code, temp_for_cls, wind, gust=gust)
        rows.append({"date": d, "tmin": tmin, "tmax": tmax, "code": code,
                     "precip": precip, "gust": gust, "wind": wind,
                     "type": wtype, "n": len(ps)})
    return rows


def _walk_rating(wtype: str) -> tuple[str, str]:
    if not wtype:
        return ("•", "нет данных")
    m = WALK_MODS.get(wtype) or {}
    mult = float(m.get("mult", 1.0))
    if mult >= 1.25:
        return ("🚶🚶🚶", "отлично")
    if mult >= 1.05:
        return ("🚶🚶", "хорошо")
    if mult >= 0.95:
        return ("🚶", "нейтрально")
    return ("🏠", "лучше дома")


def render_today(w: dict, hours: list[dict]) -> str:
    """Экран «сегодня»: крупный снимок + почасовая лента до конца суток."""
    city = WEATHER_CITY or "Камчатка"
    dt = local_now()
    wd = _WEEKDAY_FULL_RU[dt.weekday()].capitalize()
    lines = [f"🌍 <b>{esc(city)} · {wd}, {_date_label(dt.date())}</b>", ""]
    temp = w.get("temp")
    if temp is None and not w.get("live"):
        temp = _season_temp_c(dt)
    lines.append(f"{w['icon']} <b>{esc(w['name'])}</b> · {_fmt_temp(temp)}")
    if w["live"]:
        if w.get("note"):
            lines.append(esc(w["note"]))
        extra = []
        if w.get("wind_ms") is not None:
            extra.append(f"💨 {w['wind_ms']:.0f} м/с")
        if w.get("humidity") is not None:
            extra.append(f"💧 {w['humidity']:.0f}%")
        if w.get("pressure") is not None:
            hpa = round(float(w["pressure"]) * 0.7500637)
            extra.append(f"🌀 {hpa} мм рт.ст.")
        if extra:
            lines += ["", " · ".join(extra)]
    else:
        lines.append("⚠️ Живые данные недоступны — сезонная модель")
    # почасовая лента: до 12 следующих часов, строками по 4 часа
    now_utc = datetime.now(timezone.utc)
    nxt = []
    for p in hours:
        dth = _parse_hour_key(p.get("time") or "")
        if dth is None or dth < now_utc - timedelta(hours=1):
            continue
        nxt.append((dth, p))
        if len(nxt) >= 12:
            break
    if nxt:
        lines += ["", "<b>⏰ Ближайшие часы</b>"]
        row: list[str] = []
        for dth, p in nxt:
            loc = dth.astimezone(user_tz())
            icon = _DAY_ICON.get(int(p.get("code") or 0), "🌡️")
            row.append(f"{loc:%H}:00 {icon}{_fmt_temp(p.get('temp'))}")
            if len(row) == 3:
                lines.append("   " + "  ".join(row))
                row = []
        if row:
            lines.append("   " + "  ".join(row))
    return "\n".join(lines)


def render_week(rows: list[dict]) -> str:
    """Экран «на неделю вперёд»: один день — одна аккуратная строка.

    Формат строки: «Понедельник, 5 октября · ⛅ −2…+3° · дождь · 🚶 хорошо».
    Полные названия дней (никаких «пн/вт»), «Сегодня»/«Завтра» для первых двух.
    """
    city = WEATHER_CITY or "Камчатка"
    dt0 = local_now()
    today_ = dt0.date()
    if not rows:
        # Фолбэк без живых данных: честная сезонная «оценка по дням»
        # (не прогноз) — чтобы экран не был пустым.
        lines = [f"📅 <b>Погода · {esc(city)} на неделю</b>",
                 f"<i>{_date_label(today_).capitalize()}</i>", "",
                 "⚠️ Живые данные OpenWeather недоступны — ниже ориентировочная",
                 "сезонная оценка, а не прогноз. Данные обновит сервер.", ""]
        icon = WEATHER_SEASONS.get(season_for(dt0), {}).get("icon", "🌡️")
        for i in range(7):
            d = today_ + timedelta(days=i)
            mid = _season_temp_c(datetime(d.year, d.month, d.day))
            lo, hi = mid - 3, mid + 3
            head = _day_head(d, today_)
            date_part = "" if i else f", {_date_label(d)}"
            lines.append(f"<b>{head}</b>{date_part} · {icon} "
                         f"{lo:+.0f}…{hi:+.0f}°")
        return "\n".join(lines)
    lines = [f"📅 <b>Погода · {esc(city)} на неделю</b>",
             f"<i>{_date_label(today_).capitalize()}</i>", ""]
    for idx, r in enumerate(rows):
        d = r["date"]
        head = _day_head(d, today_)
        date_part = "" if idx == 0 else f", {_date_label(d)}"
        icon = _DAY_ICON.get(int(r.get("code", 2)), "🌡️")
        span = f"{_fmt_temp(r['tmin'])}…{_fmt_temp(r['tmax'])}" \
            if r["tmin"] is not None else "··"
        parts = [f"<b>{head}</b>{date_part}", f"{icon} {span}"]
        word = _type_word(r.get("type", ""))
        if word:
            parts.append(word)
        walk_icon, walk_word = _walk_rating(r.get("type", ""))
        parts.append(f"🚶 {walk_icon} {walk_word}")
        lines.append(" · ".join(parts))
        det: list[str] = []
        if r.get("precip"):
            det.append(f"🌧 осадки до {r['precip']:.0f} мм")
        if r.get("gust"):
            det.append(f"💨 порывы до {r['gust'] / 3.6:.0f} м/с")
        if det:
            lines.append("   └ " + " · ".join(det))
    return "\n".join(lines)
