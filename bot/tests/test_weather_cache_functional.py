"""Регресс: негативное шторм-окно не должно затирать успешный кэш погоды."""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname("app"))
from app.services import weather as W


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

