from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

WEB_DIR = Path(__file__).resolve().parent

app = FastAPI(title="TamaBot Control Panel", docs_url=None, redoc_url=None)

from app.web.admin_api import router as admin_router  # noqa: E402
app.include_router(admin_router)

LOCALHOST_IPS = {"127.0.0.1", "::1"}
# Пустой DASHBOARD_ALLOWED_IPS = доступ только с localhost (см. _parse_allowed)

def _parse_allowed(raw: str | None) -> tuple[set[str], list]:
    ips: set[str] = set(LOCALHOST_IPS)
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    tokens = [t for t in re.split(r"[,\s;]+", (raw or "").strip()) if t]
    # пустой список = доступ только с localhost (безопасно при DASHBOARD_HOST=0.0.0.0)
    for token in tokens:
        try:
            if "/" in token:
                networks.append(ipaddress.ip_network(token, strict=False))
            else:
                ips.add(str(ipaddress.ip_address(token)))
        except ValueError:
            continue
    return ips, networks

def _ip_allowed(ip: str, allow: tuple[set[str], list]) -> bool:
    ips, networks = allow
    if ip in ips:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)

def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    peer = (peer or "").strip()
    if peer.lower().startswith("ws:") or peer.lower().startswith("tcp:"):
        peer = peer.split(":", 1)[1]
    try:
        trust_proxy = bool(get_settings_cached().dashboard_trust_proxy)
    except Exception:
        trust_proxy = False
    if trust_proxy:
        fwd = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip") or ""
        first = fwd.split(",")[0].strip()
        if first:
            return first
    return peer

_ENV_FILE_CACHE: dict = {"path": None}


def _env_file() -> Path | None:
    """Путь к .env — тот же резолвер, что использует редактор настроек панели."""
    if _ENV_FILE_CACHE["path"] is not None and _ENV_FILE_CACHE["path"] != "none":
        return _ENV_FILE_CACHE["path"]  # type: ignore[return-value]
    try:
        from app.console.settings_store import env_path
        f = env_path()
    except Exception:
        f = Path.cwd() / ".env"
    if f.is_file():
        _ENV_FILE_CACHE["path"] = f
        return f
    _ENV_FILE_CACHE["path"] = "none"
    return None


def _env_value(key: str) -> str | None:
    """Значение из файла .env напрямую (без кэша pydantic-settings)."""
    f = _env_file()
    if not f:
        return None
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _resolve_bind(host_cli: str | None, port_cli: int | None) -> tuple[str, int]:
    """Хост/порт панели: CLI > env (.env напрямую) > настройки приложения > умолчания.

    Если DASHBOARD_HOST задан как внешний IP машины — привязка к нему падает
    (IP выдаётся NAT-шлюзом провайдера), поэтому такие значения заменяются на
    0.0.0.0 с предупреждением.
    """
    from loguru import logger

    host = host_cli or os.environ.get("DASHBOARD_HOST") or _env_value("DASHBOARD_HOST")
    port_raw = (port_cli if port_cli not in (None, 8765)
                else os.environ.get("DASHBOARD_PORT") or _env_value("DASHBOARD_PORT"))
    if not host or not port_raw:
        try:
            s = get_settings_cached()
            host = host or s.dashboard_host
            port_raw = port_raw or str(s.dashboard_port)
        except Exception:
            pass
    host = (host or "127.0.0.1").strip()
    try:
        port = int(port_raw or 8765)
    except ValueError:
        port = 8765
    try:
        addr = ipaddress.ip_address(host)
        if not addr.is_unspecified and not addr.is_loopback:
            logger.warning(
                "DASHBOARD_HOST={} — это адрес интерфейса/NAT, а не локальный адрес. "
                "Привязка к нему невозможна, слушаем 0.0.0.0 (доступ по белому списку IP)",
                host)
            host = "0.0.0.0"
    except ValueError:
        pass  # hostname — оставляем как есть
    return host, port


def get_settings_cached():
    from app.config import get_settings
    return get_settings()

def _allowlist() -> tuple[set[str], list]:
    try:
        raw = get_settings_cached().dashboard_allowed_ips
    except Exception:
        raw = ""
    return _parse_allowed(raw)

@app.middleware("http")
async def ip_allowlist_middleware(request: Request, call_next):
    if not _ip_allowed(_client_ip(request), _allowlist()):
        return JSONResponse({"detail": "Доступ запрещён: ваш IP не в белом списке"},
                            status_code=403)
    # помечаем запрос как допущенный интерфейсом панели (для /api/token)
    request.state.allowed = True
    return await call_next(request)

class LogHub:

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach(self) -> None:
        from app.console.runtime import ui_log_handler
        self._loop = asyncio.get_running_loop()
        ui_log_handler.set_callback(self._on_line)

    def _on_line(self, line: str) -> None:
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
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def add(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        from app.console.runtime import ui_log_handler
        tail = list(ui_log_handler.buffer)[-500:]
        await ws.send_json({"type": "bulk", "lines": tail})

    def remove(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

hub = LogHub()

def _runtime():
    from app.console.runtime import runtime
    return runtime

def _mask_db(url: str) -> str:
    return re.sub(r"://([^:/@]+):[^@]+@", r"://\1:•••@", url)

def _status_payload() -> dict:
    from app.config import __version__
    rt = _runtime()
    st = rt.state
    up = rt.uptime_sec if st == "running" else 0.0
    me = None
    if rt.bot is not None:
        try:
            me_bot = rt.bot._me
            if me_bot is not None:
                me = {"username": me_bot.username, "id": me_bot.id,
                      "first_name": me_bot.first_name}
        except Exception:
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
    except Exception as exc:
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

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "static" / "index.html")

@app.get("/api/status")
async def status() -> JSONResponse:
    return JSONResponse(_status_payload())

class Action(BaseModel):
    action: str

@app.post("/api/control")
async def control(body: Action) -> dict:
    rt = _runtime()
    act = body.action.lower()
    if act == "start":
        if rt.state != "stopped":
            raise HTTPException(409, f"бот уже в состоянии {rt.state!r}")
        try:
            await rt.start()
        except Exception as exc:
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
        except Exception as exc:
            raise HTTPException(500, f"не удалось перезапустить: {exc}") from exc
        return {"ok": True, "state": rt.state}
    raise HTTPException(400, f"неизвестное действие {body.action!r}")

@app.get("/api/settings")
async def get_settings_api() -> dict:
    from app.console.settings_store import settings_view
    try:
        return settings_view()
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc

class SettingsPatch(BaseModel):
    updates: dict[str, str]

HOT_KEYS = {
    "LOG_LEVEL", "WEATHER_ENABLED", "MERCH_URL", "MERCH_ITEMS",
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
    except Exception:
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
    get_settings.cache_clear()
    try:  # WEATHER_* применяются без рестарта
        from app.services import weather as _w
        _w.refresh_geo()
    except Exception:
        pass
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
    lvl = str(body.get("level", "INFO")).upper()
    if lvl not in ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"):
        raise HTTPException(422, f"неизвестный уровень {lvl!r}")
    _apply_log_level(lvl)
    from loguru import logger
    logger.info("🔧 уровень live-логов оболочки изменён на {}", lvl)
    return {"ok": True, "level": lvl}

@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket) -> None:
    if not _ip_allowed(_client_ip(ws), _allowlist()):
        await ws.close(code=4403)
        return
    await hub.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.remove(ws)
    except Exception:
        hub.remove(ws)

def _browser_host(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "::") else host

def _open_browser(url: str) -> None:
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass

async def amain(host: str | None, port: int | None, autostart: bool, open_browser: bool) -> int:
    host, port = _resolve_bind(host, port)
    import uvicorn
    from loguru import logger

    from app.config import get_settings
    from app.console.runtime import runtime, setup_file_logging

    settings = get_settings()
    setup_file_logging(settings.log_level)
    hub.attach()
    url = f"http://{_browser_host(host)}:{port}"
    logger.info("🖥  панель управления: {} (Ctrl+C — остановить всё)", url)

    if autostart and settings.bot_token and settings.bot_token != "test":
        try:
            await runtime.start()
        except Exception as exc:
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
    parser.add_argument("--host", default=None,
                        help="адрес прослушивания (по умолчанию из .env DASHBOARD_HOST)")
    parser.add_argument("--port", type=int, default=None,
                        help="порт панели (по умолчанию из .env DASHBOARD_PORT)")
    parser.add_argument("--no-open", action="store_true", help="не открывать браузер")
    parser.add_argument("--no-start", action="store_true", help="не запускать бота автоматически")
    args = parser.parse_args()
    code = asyncio.run(amain(args.host, args.port,
                             autostart=not args.no_start,
                             open_browser=not args.no_open))
    raise SystemExit(code)

if __name__ == "__main__":
    main()
