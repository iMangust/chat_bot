from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

KAMCHATKA_TZ = timezone(timedelta(hours=12), name="MSK+9 (Камчатка)")

def now() -> datetime:
    return datetime.now(KAMCHATKA_TZ)

def today() -> date:
    return now().date()

def localize(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KAMCHATKA_TZ)
    return dt.astimezone(KAMCHATKA_TZ)
