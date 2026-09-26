"""Локальное время бота: Камчатский часовой пояс (Asia/Kamchatka, UTC+12, без перехода на летнее время).

Правило проекта: вся пользовательская логика (праздники, погода, расписание,
«сон до HH:MM») оперирует камчатским временем. В БД по-прежнему храним
aware-datetime с смещением — оно теперь камчатское, сравнения корректны.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# Камчатка: UTC+12, переходов на летнее время нет с 2014 года — фиксированное смещение надёжнее zoneinfo.
KAMCHATKA_TZ = timezone(timedelta(hours=12), name="MSK+9 (Камчатка)")


def now() -> datetime:
    """Текущее камчатское время (aware)."""
    return datetime.now(KAMCHATKA_TZ)


def today() -> date:
    """Текущая календарная дата по Камчатке (важно для праздников)."""
    return now().date()


def localize(dt: datetime) -> datetime:
    """Перевести datetime в камчатскую зону (naive считаем уже локальным)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KAMCHATKA_TZ)
    return dt.astimezone(KAMCHATKA_TZ)
