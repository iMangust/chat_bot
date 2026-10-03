"""Регресс: негативное шторм-окно не должно затирать успешный кэш погоды."""
import asyncio, os, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname("app"))
from app.services import weather as W
from app.utils.local_time import KAMCHATKA_TZ


def _yesterday_hour_key() -> str:
    """Ключ 'YYYY-MM-DDTHH' (UTC) за вчерашний день — как у живого /2.5/forecast,
    который всегда покрывает текущие камчатские сутки."""
    dt = datetime.now(timezone.utc) - timedelta(days=1)
    return dt.strftime("%Y-%m-%dT%H")


def test_weather_storm_window_does_not_evict_good_cache():
    return asyncio.run(_main())


async def _main():
    W._reset_state_for_tests()

    async def fake_ok():
        return {"temp": 3.2, "code": 804, "wind": 10, "hourly": {
            "time": ["2026-10-02T00:00", "2026-10-02T03:00"],
            "temp": [1.0, 2.0], "code": [2, 3], "precip": [0.0, 0.1],
            "gust": [5.0, 6.0], "cloud": [50, 70]}}

    async def fake_fail():
        return None

    # 1) успешный fetch -> живые данные
    orig = W.fetch_real_weather
    W.fetch_real_weather = fake_ok
    try:
        info = await W._ensure_fresh(force=True)
        assert info is not None, "первый успешный fetch должен вернуть данные"
        assert await W.hourly_points(), "почасовые точки должны быть закэшированы"

        # 2) имитируем залипание ошибки: ставим негативное окно как после серии фейлов
        W._cache["next_try_mono"] = W._mono.monotonic() + W.RETRY_AFTER_SEC

        # 3) даже в шторм-окне успешные данные остаются доступны
        info2 = await W._ensure_fresh()
        assert info2 is not None, "FIX: успешный кэш не должен затираться негативным окном"
        pts = await W.hourly_points()
        assert pts, "FIX: недельный экран не должен откатываться к сезонной модели"

        # 4) защита от спама сохраняется: без данных сети шторм-окно не пуска fetch
        W._reset_state_for_tests()
        W.fetch_real_weather = fake_fail
        r1 = await W._ensure_fresh()
        assert r1 is None
        calls = 0
        async def counting():
            nonlocal calls; calls += 1; return None
        W.fetch_real_weather = counting
        r2 = await W._ensure_fresh()  # внутри RETRY_AFTER_SEC — нового запроса быть не должно
        assert r2 is None and calls == 0, f"шторм-окно должно блокировать повторные запросы (calls={calls})"
    finally:
        W.fetch_real_weather = orig
    print("ALL WEATHER CACHE ASSERTS PASSED")



class TestWeekHoursSurviveNoHourlySnapshot:
    """Регресс: «неделя» откатывалась к сезонной оценке, хотя живые данные были.

    Причины (все закрыты):
    1) успешный снимок без hourly (One Call 401 на бесплатном тарифе,
       /2.5/forecast упал) принудительно затирал _hours_cache;
    2) если основной источник отдаёт снимок без почасовых точек вообще,
       часовые точки теперь дотягиваются отдельным запросом
       fetch_forecast_hours() (раз в TTL, не чаще).
    """

    def test_no_hourly_snapshot_keeps_cached_points(self):
        import asyncio
        from app.services import weather as W

        async def scenario():
            W._reset_state_for_tests()
            os.environ["OPENWEATHER_API_KEY"] = "test-key"

            async def with_hours():
                # Точка за текущие камчатские сутки: /2.5/forecast всегда их
                # покрывает (~4 дня вперёд). Проверяем, что даже один живой
                # день рендерится как прогноз, а НЕ как сезонная заглушка.
                now_k = datetime.now(KAMCHATKA_TZ)
                key = now_k.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")
                return {"temperature": 3.0, "hourly": {
                    "time": [key], "temp": [2.0],
                    "code": [61], "precip": [0.4], "gust": [18.0]}}

            async def without_hours():
                return {"temperature": 3.0, "hourly": {}}

            W.fetch_real_weather = with_hours
            await W._ensure_fresh(force=True)
            assert len(W._hours_cache["points"]) == 1
            W.fetch_real_weather = without_hours
            await W._ensure_fresh(force=True)
            assert len(W._hours_cache["points"]) == 1, \
                "снимок без hourly не должен затирать живые часовые точки"
            # Ровно тот путь, которым идёт экран «Неделя»: через hourly_points().
            points = await W.hourly_points()
            assert len(points) == 1, \
                "часовой кэш переживает шторм-окно и не пересобирается заново"
            rows = W._day_rows(points)
            assert rows, "живая почасовая точка должна давать строку дня"
            week = W.render_week(rows)
            assert "сезонная оценка" not in week, week
            W._reset_state_for_tests()

        asyncio.run(scenario())

    def test_standalone_forecast_backfills_week(self):
        import asyncio
        from app.services import weather as W

        async def scenario():
            W._reset_state_for_tests()
            os.environ["OPENWEATHER_API_KEY"] = "test-key"

            async def snap_no_hr():
                return {"temperature": 3.0, "hourly": {}}

            calls = []

            async def fake_fc():
                calls.append(1)
                return [{"time": f"2026-10-{d:02d}T06", "temp": 2.0,
                         "code": 61, "precip": 0.4, "gust": 18.0,
                         "cloud": 90} for d in range(2, 8)]

            W.fetch_real_weather = snap_no_hr
            W.fetch_forecast_hours = fake_fc
            pts = await W.hourly_points()
            assert len(pts) == 6 and len(calls) == 1
            await W.hourly_points()  # из кэша — без нового запроса
            assert len(calls) == 1
            txt = W.render_week(W._day_rows(pts))
            assert "сезонная оценка" not in txt
            W._reset_state_for_tests()

        asyncio.run(scenario())
