"""Запуск бота через веб-панель (app.console.runtime) обязан регистрировать
events.router и применять миграции схемы — иначе кнопки «📅 Мероприятия» и
«📅 Управление мероприятиями» молчат, хотя код в app/main.py их обрабатывает.

Реальный баг (июль 2026): на сервере бот запускался через панель
(python -m app.web.server -> app.console.runtime.BotRuntime.start), которая
подключала 13 роутеров БЕЗ events.router. Все фиксы в app/handlers/events.py
просто не участвовали в работе — Telegram получал callback menu:events,
aiogram не находил обработчик, и кнопка «не реагировала».
"""
import asyncio
import inspect


def test_runtime_start_includes_events_router():
    from app.console import runtime as rt_mod
    src = inspect.getsource(rt_mod.BotRuntime.start)
    assert "events.router" in src, \
        "events.router не подключён в BotRuntime.start — кнопка " \
        "'Мероприятия' мертва при запуске из панели"


def test_runtime_applies_schema_migrations():
    from app.console import runtime as rt_mod
    src = inspect.getsource(rt_mod.BotRuntime.start)
    assert "ensure_events_table" in src, \
        "миграция таблицы events не вызывается при запуске из панели"
    assert "_light_migrations" in src, \
        "лёгкие миграции колонок не применяются при запуске из панели"


def _matched_handler_name(router, cb):
    """Прогоняем фильтры зарегистрированных callback-хендлеров роутера."""
    for h in router.callback_query.handlers:
        data = asyncio.run(h.check(cb))
        if data is not False and data is not None:
            return getattr(h.callback, "__name__", str(h.callback))
    return None


def _cb(data):
    from aiogram.types import CallbackQuery, User
    return CallbackQuery(id="x", chat_instance="ci", data=data,
                         from_user=User(id=1, is_bot=False, first_name="t"))


def test_events_router_matches_menu_and_admin_callbacks():
    from app.handlers import events as events_h
    assert _matched_handler_name(events_h.router, _cb("menu:events")) == "menu_events"
    assert _matched_handler_name(events_h.router, _cb("evadmin:home")) is not None
    # fallback для неизвестных menu:* тоже должен матчиться (без «мёртвых» кнопок)
    assert _matched_handler_name(events_h.router, _cb("menu:something_new")) is not None
