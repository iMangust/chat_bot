"""Live-режим оболочки: консольный запуск бота с живыми логами в реальном времени.

Это основной способ запуска по двойному клику на run.bat:
* логuru пишет и в консоль (цветной, живой), и в logs/bot_YYYY-MM-DD.log;
* заголовок окна консоли обновляется статусом (🟢 работает / 🔴 остановлен + uptime);
* Ctrl+C или закрытие окна → graceful shutdown (планировщик → поллинг → Redis/engine);
* после нештатного завершения окно «держится» паузой, чтобы оператор успел прочитать ошибку.

Запуск напрямую:  python -m app.console.livelog   (из каталога bot/)
"""
from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
import time

from loguru import logger

from app.config import __version__, get_settings
from app.console.runtime import runtime


# ------------------------------------------------------------- console sink
def _console_sink(message) -> None:
    """Живой цветной вывод в консоль (то же форматирование, что у aiogram-логов)."""
    record = message.record
    level = record["level"]
    color = {
        "DEBUG": "\033[90m", "INFO": "\033[36m", "SUCCESS": "\033[32m",
        "WARNING": "\033[33m", "ERROR": "\033[1;31m", "CRITICAL": "\033[1;97;41m",
    }.get(level.name, "")
    line = (f"\033[90m{record['time']:HH:mm:ss}\033[0m {color}{level.name:<7}\033[0m "
            f"\033[90m{record['name']}\033[0m - {color}{record['message']}\033[0m")
    print(line, flush=True)


def setup_live_logging(level: str) -> None:
    """Консоль (живая) + файл логов. Отдельно от GUI-настройки в runtime."""
    logger.remove()
    # Консоль Windows работает в cp1251 (run.bat ставит chcp 1251).
    # Python по умолчанию пишет UTF-8 -> cmd показывает «иероглифы».
    # Приводим stdout к активной кодовой странице консоли; при любой
    # ошибке оставляем как есть (Linux/терминалы с UTF-8 работают нормально).
    with contextlib.suppress(Exception):
        import locale
        enc = (locale.getpreferredencoding(False) or "utf-8").lower()
        if enc not in ("utf-8", "utf8") and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding=enc, errors="replace")
    logger.add(_console_sink, level=level)
    from pathlib import Path
    Path("logs").mkdir(exist_ok=True)
    logger.add("logs/bot_{time:YYYY-MM-DD}.log", rotation="1 day",
               retention="14 days", level="DEBUG", encoding="utf-8")


# --------------------------------------------------------------- title bar
def set_console_title(title: str) -> bool:
    """Заголовок окна консоли: WinAPI (для run.bat) или ANSI escape (fallback)."""
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
            return True
        sys.stdout.write(f"\x1b]0;{title}\x07")
        sys.stdout.flush()
        return True
    except Exception:  # noqa: BLE001 — украшательство, ронять ради него бота нельзя
        return False


async def _title_updater(stop: asyncio.Event) -> None:
    """Пока бот жив, раз в 5 секунд обновляем заголовок окна статусом."""
    while not stop.is_set():
        st = {"stopped": "🔴 остановлен", "starting": "🟡 запуск…",
              "running": "🟢 работает", "stopping": "🟡 остановка…"}.get(runtime.state, "❓")
        up = ""
        if runtime.state == "running" and runtime.started_at:
            s = int(time.time() - runtime.started_at)
            up = f" | up {s // 3600}ч {(s % 3600) // 60}м {s % 60}с"
        err = f" | ⚠ {runtime.last_error[:40]}" if runtime.last_error else ""
        set_console_title(f"🐾 TamaBot v{__version__} — {st}{up}{err}")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=5.0)


# -------------------------------------------------------------------- main
async def amain() -> int:
    """Точка live-режима: старт → ожидание сигнала/падения поллинга → стоп."""
    settings = get_settings()
    setup_live_logging(settings.log_level)
    logger.info("🐾 TamaBot — live-режим (логи в реальном времени, Ctrl+C для остановки)")

    try:
        await runtime.start()
    except Exception as exc:  # noqa: BLE001
        logger.error("не удалось запустить бота: {}", exc)
        return 2

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
        # Windows: SIGINT приходит как KeyboardInterrupt — его ловит main() ниже

    title_task = asyncio.create_task(_title_updater(stop), name="title-updater")

    async def _watchdog() -> None:
        """Если поллинг упал сам — завершаем live-режим с кодом ошибки."""
        task = runtime._polling_task
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if runtime.state == "stopped" and runtime.last_error:
                stop.set()

    watchdog = asyncio.create_task(_watchdog(), name="polling-watchdog")
    await stop.wait()
    watchdog.cancel()
    title_task.cancel()
    logger.info("получен сигнал остановки…")
    await runtime.stop()
    return 0


def main() -> None:
    """Синхронная обёртка с graceful shutdown по Ctrl+C и паузой после ошибок."""
    code = 0
    try:
        code = asyncio.run(amain())
    except KeyboardInterrupt:
        print("\n⛔ Ctrl+C — останавливаю бота…", flush=True)
        with contextlib.suppress(Exception):
            asyncio.run(runtime.stop())
    except Exception as exc:  # noqa: BLE001
        logger.critical("нештатное завершение: {}", exc)
        code = 1
    finally:
        set_console_title("TamaBot — остановлен")
        hold = "--no-hold" not in sys.argv and (not sys.stdin or sys.stdin.isatty())
        if code != 0 and hold:
            with contextlib.suppress(Exception):
                input("\nНажмите Enter, чтобы закрыть окно…")
        sys.exit(code)


if __name__ == "__main__":
    main()
