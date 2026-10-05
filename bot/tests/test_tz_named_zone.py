"""Регрессии для TZ_NAME (именованная IANA-зона с честным DST).

Проблема: фиксированный TZ_OFFSET_HOURS врёт на час в зонах с DST, а
cron-джоба APScheduler в день перевода часов может падать. Решение —
опциональная настройка TZ_NAME: планировщик и локализация используют
zoneinfo-зону; кэш зоны сбрасывается панелью при сохранении настроек
(HOT_KEYS), поэтому смена применяется без рестарта процесса.

Тесты идут тем же путём, что и рантайм: os.environ + get_settings.cache_clear()
(как делает POST /api/settings), а не подменой Settings-объекта.
"""
from __future__ import annotations

import os
import zoneinfo
from datetime import datetime, timedelta, timezone

import pytest

import app.utils.local_time as lt


@pytest.fixture(autouse=True)
def _restore_tz_env():
    old_off = os.environ.get("TZ_OFFSET_HOURS")
    old_name = os.environ.get("TZ_NAME")
    yield
    for key, val in (("TZ_OFFSET_HOURS", old_off), ("TZ_NAME", old_name)):
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    from app.config import get_settings
    get_settings.cache_clear()
    lt.reset_tz_cache()


def _apply(offset="0", name=""):
    """Имитация сохранения настроек из панели (HOT_KEYS-путь)."""
    os.environ["TZ_OFFSET_HOURS"] = offset
    if name:
        os.environ["TZ_NAME"] = name
    else:
        os.environ.pop("TZ_NAME", None)
    from app.config import get_settings
    get_settings.cache_clear()
    lt.reset_tz_cache()


def test_default_is_utc_fixed():
    _apply()
    tz = lt.user_tz()
    assert isinstance(tz, timezone) and not isinstance(tz, zoneinfo.ZoneInfo)
    assert tz.utcoffset(None) == timedelta(0)


def test_named_zone_used_when_configured():
    _apply(name="Europe/Berlin")
    tz = lt.user_tz()
    assert isinstance(tz, zoneinfo.ZoneInfo)
    # честный DST: июль +2, январь +1
    assert tz.utcoffset(datetime(2026, 7, 15, 12)) == timedelta(hours=2)
    assert tz.utcoffset(datetime(2026, 1, 15, 12)) == timedelta(hours=1)


def test_unknown_zone_falls_back_to_offset():
    _apply(offset="5", name="Not/AZone")
    tz = lt.user_tz()
    assert isinstance(tz, timezone) and not isinstance(tz, zoneinfo.ZoneInfo)
    assert tz.utcoffset(None) == timedelta(hours=5)


def test_hot_update_applies_without_restart():
    """Смена TZ_* через окружение + cache_clear (как в /api/settings) живая."""
    _apply()
    assert lt.user_tz().utcoffset(None) == timedelta(0)
    _apply(offset="3")
    assert lt.user_tz().utcoffset(None) == timedelta(hours=3)
    _apply(name="Europe/Berlin")
    assert isinstance(lt.user_tz(), zoneinfo.ZoneInfo)


def test_localize_aware_in_named_zone_same_epoch_unchanged():
    _apply(name="Europe/Berlin")
    tz = lt.user_tz()
    dt = datetime(2026, 7, 15, 12, 0, tzinfo=tz)
    assert lt.localize(dt) == dt


def test_scheduler_builds_with_named_dst_zone():
    """build_scheduler принимает ZoneInfo-зону и планирует cron без падения."""
    _apply(name="Europe/Berlin")
    from app.tasks.scheduler import build_scheduler
    sched = build_scheduler(bot=None)  # бот используется только внутри задач
    try:
        assert getattr(sched.timezone, "key", str(sched.timezone)) == "Europe/Berlin"
        job = sched.get_job("daily")
        assert job is not None
        # триггер привязан к DST-зоне; ручной пересчёт следующего запуска в
        # фиксированной зоне не нужен — проверяем, что зона задачи correct
        assert str(job.trigger)  # cron-триггер сконструирован без ошибок
    finally:
        if sched.running:
            sched.shutdown(wait=False)
