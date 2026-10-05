from __future__ import annotations

import zoneinfo
from datetime import date, datetime, timedelta, timezone

# Настроенная именованная IANA-зона (TZ_NAME): честный DST вместо жёсткого
# смещения. Кэш сбрасывается при смене имени зоны (панель может менять без
# рестарта). Пусто/неизвестное имя — фиксированный TZ_OFFSET_HOURS.
_TZ_CACHE: tuple[tuple[str | None, int] | None, timezone] = (None, timezone.utc)


def reset_tz_cache() -> None:
    """Сброс кэша зоны после изменения TZ_OFFSET_HOURS/TZ_NAME без рестарта."""
    global _TZ_CACHE
    _TZ_CACHE = (None, timezone.utc)


def _configured_zone_name() -> str | None:
    try:
        from app.config import get_settings
        name = str(getattr(get_settings(), "tz_name", "") or "").strip()
        return name or None
    except Exception:
        return None


def user_tz() -> timezone:
    """Фиксированная зона пользователей из настроек (TZ_OFFSET_HOURS от UTC).

    По умолчанию — UTC (0); историческая конфигурация — Камчатка (UTC+12),
    но зона больше не зашитая: при изменении TZ_OFFSET_HOURS вся временная
    логика (хранилище, планировщик, дни стриков) следует за настройкой.
    KAMCHATKA_TZ оставлен как псевдоним для совместимости.

    Если задан TZ_NAME (имя IANA-зоны, например Europe/Berlin) —
    возвращается настоящая tzinfo с DST-правилами: фиксированный offset на
    таких зонах зимой/летом врёт на час, а cron в день перевода часов падает
    с NonExistentTimeError. Иначе — фиксированное смещение TZ_OFFSET_HOURS
    (дефолт 0 = UTC).
    Ключ кэша включает и TZ_OFFSET_HOURS: панель применяет настройки без
    рестарта, и смена offset при пустом TZ_NAME тоже должна пересобрать зону.
    """
    global _TZ_CACHE
    name = _configured_zone_name()
    cache_key = (name, offset_hours())
    if _TZ_CACHE[0] != cache_key:
        tz: timezone | zoneinfo.ZoneInfo
        if name:
            try:
                tz = zoneinfo.ZoneInfo(name)
            except Exception:
                # неизвестное имя зоны: не роняем бота, работаем по offset
                tz = timezone(timedelta(hours=offset_hours()),
                              name=f"UTC{offset_hours():+d}")
        else:
            tz = timezone(timedelta(hours=offset_hours()),
                          name=f"UTC{offset_hours():+d}")
        _TZ_CACHE = (cache_key, tz)
    return _TZ_CACHE[1]


def _is_named_zone(tz) -> bool:
    """True для zoneinfo-зон (с DST); False для фиксированного datetime.timezone."""
    return isinstance(tz, zoneinfo.ZoneInfo)


KAMCHATKA_TZ = timezone(timedelta(hours=12), name="MSK+9 (Камчатка)")  # псевдоним/фолбэк


def now() -> datetime:
    """Текущий момент в локальном времени пользователей (TZ_OFFSET_HOURS)."""
    return datetime.now(user_tz())


def utc_now() -> datetime:
    """Текущий момент в UTC — единственный источник для колонок created_at и т.п."""
    return datetime.now(timezone.utc)


def today() -> date:
    return now().date()


def localize(dt: datetime) -> datetime:
    """Перевод момента в локальное отображение (TZ_OFFSET_HOURS).

    Aware-значения переводятся штатно. Naive-метки из БД — это текущее
    хранилище (локальное время пользователей): возвращаем как есть. Эвристика
    совместимости осталась только для периода UTC-хранения (промежуточные
    сборки писали created_at в UTC): если naive-момент выглядит как
    «будущее» для локали (например, UTC-полдень при локальных 03:00),
    считаем его UTC и сдвигаем на +offset часов.
    """
    tz = user_tz()
    if dt.tzinfo is not None:
        # Aware-метки бывают двух видов: «настоящий момент» (datetime.now(utc),
        # message.date из Telegram) — их переводим astimezone; и псевдо-aware
        # локальные метки now() c фиксированным offset — их нормализуем к зоне
        # настроек (при TZ_NAME с DST offset мог быть снят в другую эпоху).
        off = dt.utcoffset() or timedelta(0)
        named = _is_named_zone(tz)
        if not named and off == timedelta(hours=offset_hours()):
            return dt  # уже в нужной фиксированной зоне
        if named and off == dt.astimezone(tz).utcoffset():
            return dt  # уже в нужной именованной зоне (та же DST-эпоха)
        return dt.astimezone(tz)
    local_naive = now().replace(tzinfo=None)
    if dt > local_naive + timedelta(hours=6):
        # явное «будущее» для локали → метка эпохи UTC-хранения
        return (dt + timedelta(hours=offset_hours())).replace(tzinfo=tz)
    return dt.replace(tzinfo=tz)


def as_utc(dt: datetime) -> datetime:
    """Момент в UTC. Naive-значения считаются локальными (см. localize)."""
    return localize(dt).astimezone(timezone.utc)


def db_bound(dt: datetime) -> datetime:
    """Граница для SQL-сравнений с колонками created_at/last_seen.

    Колонки хранят локальное время пользователей (TZ_OFFSET_HOURS, см.
    models.utcnow), поэтому aware-границы приводим к локали и снимаем зону;
    naive считаются уже локальными — возвращаем как есть.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(user_tz())
    return dt.replace(tzinfo=None)


def from_iso(value: str | None) -> datetime | None:
    """Разбор ISO-метки, приводимый к локальной зоне пользователей.

    Метку без зоны считаем локальной (так писалось до унификации), чтобы
    сравнения с aware-«сейчас» не давали сдвига на tz_offset часов.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return localize(dt)


def offset_hours() -> int:
    """Часовой пояс пользователей относительно UTC из настроек (по умолчанию UTC=0)."""
    try:
        from app.config import get_settings
        return int(get_settings().tz_offset_hours)
    except Exception:
        return 0


def user_local(dt: datetime) -> datetime:
    """Момент в локальном времени пользователя (TZ_OFFSET_HOURS от UTC).

    Naive-значения считаются UTC — так приходят message.date из aiogram,
    если кто-то передал их без явной зоны.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt + timedelta(hours=offset_hours())
