"""Регресс: игры не должны «зависать на первом же шаге».

Корневая причина (2026-10): экраны мини-игр вызывают svc.render_async(),
который идёт в local_weather() (историческое имя — kamchatka_weather()).
При протухшем кэше тот синхронно ждал сетевой fetch (до 6+10 сек на
источник, при недоступности сети — ещё дольше). aiogram не успевал ответить
на callback за отведённые Telegram'ом 10 секунд → тап по любому ходу
(«rps:rock», «bj:hit», «guess:7») выглядел как «кнопка не реагирует»: игра
стартует, а первый шаг зависает.

Фикс: local_weather НИКОГДА не блокируется на сеть дольше короткого лимита;
наружу всегда отдаётся кэш/сезонная модель. Тест ниже имитирует «сеть висит
30 секунд» и требует мгновенного возврата.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname("app"))
from app.services import weather as W  # noqa: E402


def test_local_weather_never_blocks_on_slow_network():
    return asyncio.run(_main())


async def _main():
    W._reset_state_for_tests()

    async def hang_forever():
        await asyncio.sleep(30)   # имитация зависшей сети / медленного API
        return None

    orig = W.fetch_real_weather
    W.fetch_real_weather = hang_forever
    try:
        done, pending = await asyncio.wait(
            [asyncio.create_task(W.local_weather())], timeout=5.0)
        for t in pending:
            t.cancel()
        assert done, ("local_weather() заблокировалась на сетевом fetch — "
                      "callback-хендлеры игр превысят 10-секундный лимит "
                      "Telegram и кнопки «перестанут реагировать»")
        info = done.pop().result()
        assert info.get("icon") and info.get("name"), \
            "при недоступной сети должна возвращаться сезонная модель"
    finally:
        W.fetch_real_weather = orig


def test_render_async_survives_broken_weather(monkeypatch=None):
    """render_async не должен ронять экран итога игры ни при каком погоденном
    исключении (страховочный except внутри render_async)."""
    return asyncio.run(_main2())


async def _main2():
    from app.services.tamagotchi import TamagotchiService

    class FakePet:
        name = "Тест"
        species = "cat"
        stage = "teen"
        hunger = happiness = energy = hygiene = health = 70
        level = xp = intellect = strength = agility = 1
        is_sleeping = False
        walk_until = walk_start_at = None
        settings_extra = {}
        equipped = {}
        color = "default"
        lives = 3

    async def boom():
        raise RuntimeError("сеть мертва")

    orig = W.local_weather
    W.local_weather = boom
    try:
        svc = TamagotchiService(session=None)
        text = await svc.render_async(FakePet())
        assert "Тест" in text, "экран карточки должен строиться без погоды"
    finally:
        W.local_weather = orig
