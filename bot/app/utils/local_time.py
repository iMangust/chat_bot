from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

KAMCHATKA_TZ = timezone(timedelta(hours=12), name="MSK+9 (Камчатка)")


def now() -> datetime:
    """Текущий момент в камчатском времени (UTC+12, без летнего перехода)."""
    return datetime.now(KAMCHATKA_TZ)


def utc_now() -> datetime:
    """Текущий момент в UTC — единственный источник для колонок created_at и т.п."""
    return datetime.now(timezone.utc)


def today() -> date:
    return now().date()


def localize(dt: datetime) -> datetime:
    """Перевод момента в камчатское отображение.

    Aware-значения переводятся штатно. Naive-метки из БД — это текущее
    хранилище (локальное камчатское время): возвращаем как есть. Эвристика
    совместимости осталась только для периода UTC-хранения (промежуточные
    сборки писали created_at в UTC): если naive-момент выглядит как
    «будущее» для Камчатки (например, UTC-полдень при локальных 03:00),
    считаем его UTC и сдвигаем на +12 ч.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(KAMCHATKA_TZ)
    local_naive = now().replace(tzinfo=None)
    if dt > local_naive + timedelta(hours=6):
        # явное «будущее» для Камчатки → метка эпохи UTC-хранения
        return (dt + timedelta(hours=12)).replace(tzinfo=KAMCHATKA_TZ)
    return dt.replace(tzinfo=KAMCHATKA_TZ)


def as_utc(dt: datetime) -> datetime:
    """Момент в UTC. Naive-значения считаются камчатскими (см. localize)."""
    return localize(dt).astimezone(timezone.utc)


def db_bound(dt: datetime) -> datetime:
    """Граница для SQL-сравнений с колонками created_at/last_seen.

    Колонки хранят локальное (камчатское) время (см. models.utcnow), поэтому
    aware-границы приводим к Камчатке и снимаем зону; naive считаются уже
    локальными — возвращаем как есть.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(KAMCHATKA_TZ)
    return dt.replace(tzinfo=None)


def from_iso(value: str | None) -> datetime | None:
    """Разбор ISO-метки, приводимый к камчатской зоне.

    Метку без зоны считаем камчатской (так писалось до унификации), чтобы
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
    """Часовой пояс пользователей относительно UTC из настроек (по умолчанию Камчатка)."""
    try:
        from app.config import get_settings
        return int(get_settings().tz_offset_hours)
    except Exception:
        return 12


def user_local(dt: datetime) -> datetime:
    """Момент в локальном времени пользователя (TZ_OFFSET_HOURS от UTC).

    Naive-значения считаются UTC — так приходят message.date из aiogram,
    если кто-то передал их без явной зоны.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt + timedelta(hours=offset_hours())
