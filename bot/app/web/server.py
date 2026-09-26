"""Веб-оболочка TamaBot: красивый интерфейс поверх управляемого рантайма.

Запуск:  python -m app.web   (из каталога bot/ или через run_gui.bat)

Что даёт:
* панель статуса (состояние, uptime, бот, планировщик, БД/Redis) с автообновлением;
* живые логи в реальном времени (WebSocket) с фильтрами по уровню и поиском;
* онлайн-редактор настроек .env с валидацией (сохранение → «требуется рестарт»);
* управление: старт / стоп / рестарт бота прямо из браузера.

Всё работает в одном asyncio-цикле с ботом (app.console.runtime), поэтому
логи приходят без задержек, а запуск/остановка — graceful.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

WEB_DIR = Path(__file__).resolve().parent

app = FastAPI(title="TamaBot Control Panel", docs_url=None, redoc_url=None)


# ------------------------------------------------------------- log stream
class LogHub:
    """Собирает строки из UiLogHandler и рассылает подписчикам-WS."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach(self) -> None:
        """Подключить sink loguru к hub'у (один раз при старте веб-процесса)."""
        from app.console.runtime import ui_log_handler
        self._loop = asyncio.get_running_loop()
        ui_log_handler.set_callback(self._on_line)

    def _on_line(self, line: str) -> None:
        # sink вызывается синхронно из потока лога (event loop'а) —
        # threadsafe-диспатч на случай вызова из другого потока
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._schedule, line)

    def _schedule(self, line: str) -> None:
        asyncio.ensure_future(self._broadcast(line))

    async def _broadcast(self, line: str) -> None:
        dead = []
        payload = {"type": "log", "line": line}
        for ws in list(self.clients):
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def add(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        # сразу отдаём накопленный буфер, чтобы окно логов не было пустым
        from app.console.runtime import ui_log_handler
        tail = list(ui_log_handler.buffer)[-500:]
        await ws.send_json({"type": "bulk", "lines": tail})

    def remove(self, ws: WebSocket) -> None:
        self.clients.discard(ws)


hub = LogHub()


# ---------------------------------------------------------------- helpers
def _runtime():
    from app.console.runtime import runtime
    return runtime


def _mask_db(url: str) -> str:
    """mysql+aiomysql://user:PASS@host → user:•••@host (пароль не показываем)."""
    return re.sub(r"://([^:/@]+):[^@]+@", r"://\1:•••@", url)


def _status_payload() -> dict:
    from app.config import __version__
    rt = _runtime()
    st = rt.state
    up = rt.uptime_sec if st == "running" else 0.0
    me = None
    if rt.bot is not None:
        try:
            me_bot = rt.bot._me  # заполняется после get_me()/поллинга
            if me_bot is not None:
                me = {"username": me_bot.username, "id": me_bot.id,
                      "first_name": me_bot.first_name}
        except Exception:  # noqa: BLE001
            me = None
    settings = None
    try:
        from app.config import get_settings
        s = get_settings()
        settings = {
            "botTokenSet": bool(s.bot_token and s.bot_token != "test"),
            "databaseUrl": _mask_db(s.database_url),
            "redisUrl": s.redis_url,
            "logLevel": s.log_level,
            "adminIds": s.admin_ids,
            "trackedChatIds": s.tracked_chat_ids,
            "mtprotoMode": s.mtproto_answer_mode,
            "isDev": s.is_dev,
        }
    except Exception as exc:  # noqa: BLE001
        settings = {"error": str(exc)}
    return {
        "state": st,
        "version": __version__,
        "uptimeSec": round(up, 1),
        "startedAt": rt.started_at,
        "lastError": rt.last_error,
        "bot": me,
        "jobs": rt.jobs_info(),
        "settings": settings,
        "serverTime": time.time(),
    }


# ------------------------------------------------------------------ pages
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "static" / "index.html")


@app.get("/api/status")
async def status() -> JSONResponse:
    return JSONResponse(_status_payload())


# -------------------------------------------------------------- controls
class Action(BaseModel):
    action: str  # start | stop | restart


@app.post("/api/control")
async def control(body: Action) -> dict:
    rt = _runtime()
    act = body.action.lower()
    if act == "start":
        if rt.state != "stopped":
            raise HTTPException(409, f"бот уже в состоянии {rt.state!r}")
        try:
            await rt.start()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"не удалось запустить: {exc}") from exc
        return {"ok": True, "state": rt.state}
    if act == "stop":
        if rt.state == "stopped":
            return {"ok": True, "state": rt.state}
        await rt.stop()
        return {"ok": True, "state": rt.state}
    if act == "restart":
        if rt.state != "stopped":
            await rt.stop()
        try:
            await rt.start()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"не удалось перезапустить: {exc}") from exc
        return {"ok": True, "state": rt.state}
    raise HTTPException(400, f"неизвестное действие {body.action!r}")


# --------------------------------------------------------------- settings
@app.get("/api/settings")
async def get_settings_api() -> dict:
    from app.console.settings_store import settings_view
    try:
        return settings_view()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc)) from exc


class SettingsPatch(BaseModel):
    updates: dict[str, str]


# Ключи, которые можно применить «на лету» (читаются при каждом обращении
# через get_settings()); остальные требуют рестарта бота.
HOT_KEYS = {
    "LOG_LEVEL", "WEATHER_ENABLED", "MERCH_URL", "MERCH_ITEMS",
    "CHANNEL_WELCOME_TEXT", "WELCOME_CHANNEL_ENABLED",
    "PET_WARNING_MIN_HOURS", "STREAK_WARN_THRESHOLD_SEC",
    "INVITE_REWARD_COINS", "DAILY_REPORT_HOUR_UTC",
    "MORNING_REMINDER_HOUR_UTC", "EVENING_REMINDER_HOUR_UTC",
    "TZ_OFFSET_HOURS", "CHANNEL_SCAN_MINUTES", "MIN_MESSAGE_LENGTH",
    "ACTIVITY_COOLDOWN_SEC", "REACTIONS_CAP_PER_DAY", "XP_PER_MESSAGE",
    "COINS_PER_MESSAGE_CAP", "XP_LEVEL_BASE", "MTPROTO_SYNC_MINUTES",
    "MTPROTO_AUTOSYNC",
}


@app.post("/api/settings")
async def save_settings(body: SettingsPatch) -> dict:
    from app.config import get_settings
    from app.console.settings_store import validate_updates, write_env
    try:
        old = get_settings()
    except Exception:  # noqa: BLE001
        old = None
    try:
        clean, warns = validate_updates(body.updates)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not clean:
        return {"ok": True, "changed": [], "warnings": warns,
                "restartRequired": False}
    try:
        write_env(clean)
    except OSError as exc:
        raise HTTPException(500, f"не удалось записать .env: {exc}") from exc
    # пересборка кэша настроек процесса: следующий get_settings() прочитает .env заново
    get_settings.cache_clear()
    new = get_settings()
    changed_fields = [name for name in new.model_fields
                      if old is None or getattr(new, name) != getattr(old, name)]
    restart_required = any(k.upper() not in HOT_KEYS for k in changed_fields)
    if "LOG_LEVEL" in [k.upper() for k in changed_fields]:
        _apply_log_level(new.log_level)
    return {"ok": True, "changed": sorted(k.upper() for k in changed_fields),
            "warnings": warns, "restartRequired": restart_required}


def _apply_log_level(level: str) -> None:
    from loguru import logger
    from app.console.runtime import setup_file_logging
    setup_file_logging(level)


@app.post("/api/loglevel")
async def set_log_level(body: dict) -> dict:
    """Мгновенная смена уровня live-логов оболочки без рестарта (для отладки)."""
    lvl = str(body.get("level", "INFO")).upper()
    if lvl not in ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"):
        raise HTTPException(422, f"неизвестный уровень {lvl!r}")
    _apply_log_level(lvl)
    from loguru import logger
    logger.info("🔧 уровень live-логов оболочки изменён на {}", lvl)
    return {"ok": True, "level": lvl}


# ------------------------------------------------------------------- ws
@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket) -> None:
    await hub.add(ws)
    try:
        while True:
            await ws.receive_text()  # keep-alive ping от клиента
    except WebSocketDisconnect:
        hub.remove(ws)
    except Exception:  # noqa: BLE001
        hub.remove(ws)


# ------------------------------------------------------------------ main
def _browser_host(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "::") else host


def _open_browser(url: str) -> None:
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass


async def amain(host: str, port: int, autostart: bool, open_browser: bool) -> int:
    import uvicorn
    from loguru import logger

    from app.config import __version__, get_settings
    from app.console.runtime import runtime, setup_file_logging

    settings = get_settings()
    setup_file_logging(settings.log_level)
    hub.attach()
    url = f"http://{_browser_host(host)}:{port}"
    logger.info("🖥  панель управления: {} (Ctrl+C — остановить всё)", url)

    if autostart and settings.bot_token and settings.bot_token != "test":
        try:
            await runtime.start()
        except Exception as exc:  # noqa: BLE001
            # бот не поднялся — оболочка всё равно открывается: чиним из UI
            logger.error("автозапуск бота не удался: {} — исправьте настройки в панели", exc)
    elif autostart:
        logger.warning("BOT_TOKEN не задан — панель открыта, запустите бота после настройки .env")

    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            access_log=False)
    server = uvicorn.Server(config)

    async def opener() -> None:
        if not open_browser:
            return
        while not server.started:
            await asyncio.sleep(0.1)
        _open_browser(url)

    opener_task = asyncio.create_task(opener(), name="browser-opener")
    try:
        await server.serve()
    finally:
        opener_task.cancel()
        await runtime.stop()
    return 0


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="TamaBot — веб-панель управления")
    parser.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8765")))
    parser.add_argument("--no-open", action="store_true", help="не открывать браузер")
    parser.add_argument("--no-start", action="store_true", help="не запускать бота автоматически")
    args = parser.parse_args()
    code = asyncio.run(amain(args.host, args.port,
                             autostart=not args.no_start,
                             open_browser=not args.no_open))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
