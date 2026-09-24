"""TamaConsole — терминальная оболочка (Textual) для запуска и управления ботом.

Возможности:
* ▶️ Запуск / ⏹ Остановка бота прямо из интерфейса (тот же код, что `python -m app.main`);
* 📜 Живые логи loguru с фильтрами по уровню и поиском;
* 📊 Панель статуса: uptime, состояние, задачи планировщика;
* 🗄 Сводка БД + просмотр таблиц (users, pets, achievements …) с LIMIT 200;
* ⚙️ Редактор настроек (.env): изменение на лету без потери комментариев,
  кнопка «Перезапустить» применяет настройки и перезапускает бота;
* 🔎 Поиск пользователя по нику/ID.

Запуск:  python -m app.console   (из каталога bot/, рядом должен лежать .env)
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger
from sqlalchemy import func, select
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label,
                             Log, RichLog, Static, TabbedContent, TabPane)

from app.config import __version__, get_settings
from app.console import settings_io
from app.console.runtime import runtime, setup_file_logging, ui_log_handler
from app.db.models import (Achievement, ChatMessageLog, NotificationQueue, Pet,
                           User)
from app.db.session import session_factory

STATE_LABELS = {
    "stopped": ("🔴 остановлен", "red"),
    "starting": ("🟡 запуск…", "yellow"),
    "running": ("🟢 работает", "green"),
    "stopping": ("🟡 остановка…", "yellow"),
}


def fmt_uptime(sec: float) -> str:
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}д {h}ч {m}м"
    if h:
        return f"{h}ч {m}м {s}с"
    return f"{m}м {s}с"


# ------------------------------------------------------------------ dialogs
class ConfirmQuit(ModalScreen[bool]):
    """Диалог выхода с остановкой бота."""
    CSS = """
    ConfirmQuit { align: center middle; }
    #dlg { width: 60; height: auto; padding: 1 2; border: thick $primary;
           background: $surface; }
    #dlg Horizontal { margin-top: 1; align-horizontal: right; width: 100%; }
    #dlg Horizontal Button { margin-left: 2; }
    """
    def compose(self) -> ComposeResult:
        state = "Бот сейчас запущен — остановить его перед выходом?" \
            if runtime.state == "running" else "Выйти из оболочки?"
        with Vertical(id="dlg"):
            yield Static("⚠️ Выход из TamaConsole", markup=False)
            yield Static(state, id="q")
            with Horizontal():
                yield Button("Остановить и выйти", variant="error", id="yes")
                yield Button("Отмена", variant="default", id="no")

    @on(Button.Pressed, "#yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def _no(self) -> None:
        self.dismiss(False)


class EditSetting(ModalScreen[str | None]):
    """Модальное окно редактирования одного значения настройки."""
    CSS = """
    EditSetting { align: center middle; }
    #dlg { width: 80; height: auto; padding: 1 2; border: thick $primary;
           background: $surface; }
    Input { margin-top: 1; }
    """

    def __init__(self, key: str, title: str, typ: str, current: str, hint: str) -> None:
        super().__init__()
        self.key, self.title, self.typ, self.hint = key, title, typ, hint
        # секреты не показываем — только запись нового значения
        self.current = "" if self.key in settings_io.SECRET_KEYS else current

    def compose(self) -> ComposeResult:
        placeholders = {"bool": "true / false", "int": "число",
                        "list[int]": "-100123,-100456", "secret": "введите новый токен"}
        with Vertical(id="dlg"):
            yield Static(f"[b]{self.title}[/b]  [dim]({self.key})[/dim]")
            if self.hint:
                yield Static(f"[dim]подсказка: {self.hint}[/dim]")
            yield Input(value=self.current,
                        placeholder=placeholders.get(self.typ, ""), id="val")
            yield Static("", id="err")
            with Horizontal():
                yield Button("Сохранить", variant="primary", id="ok")
                yield Button("Отмена", variant="default", id="cancel")

    @on(Input.Submitted)
    def _submit(self) -> None:
        self._save()

    @on(Button.Pressed, "#ok")
    def _save(self) -> None:
        raw = self.query_one("#val", Input).value
        try:
            settings_io.parse_value(raw, self.typ)
        except ValueError as exc:
            self.query_one("#err", Static).update(f"[red]Ошибка: {exc}[/red]")
            return
        self.dismiss(raw.strip())

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


# -------------------------------------------------------------------- panels
class StatusPanel(Static):
    """Верхняя панель: версия, состояние, uptime, время."""

    state_text: reactive[str] = reactive("🔴 остановлен")
    uptime_text: reactive[str] = reactive("")

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static(f"🐾 [b]TamaConsole v{__version__}[/b]", id="brand")
            yield Static("", id="state", classes="state")
            yield Static("", id="uptime")
            yield Static("", id="clock-spacer")  # прижимает часы вправо
            yield Static("", id="clock", classes="clock")

    def watch_state_text(self, value: str) -> None:
        try:
            self.query_one("#state", Static).update(value)
        except Exception:  # noqa: BLE001 — watch при mount
            pass

    def watch_uptime_text(self, value: str) -> None:
        try:
            self.query_one("#uptime", Static).update(f"Uptime: {value}" if value else "")
        except Exception:  # noqa: BLE001
            pass

    def refresh_now(self) -> None:
        """Обновление панели. Вызывается по таймеру — не должен ронять приложение,
        поэтому каждый query_one защищён (виджеты могут быть в процессе ремоунта)."""
        label, _color = STATE_LABELS.get(runtime.state, ("❓", "white"))
        err = f" · последняя ошибка: {runtime.last_error[:60]}" if runtime.last_error else ""
        self.state_text = f"{label}{err}"
        self.uptime_text = fmt_uptime(runtime.uptime_sec) if runtime.state == "running" else ""
        try:
            self.query_one("#clock", Static).update(time.strftime("%H:%M:%S"))
        except Exception:  # noqa: BLE001 — виджет ещё/уже не примонтирован
            pass


class ControlsBar(Static):
    """Кнопки глобального управления."""

    def compose(self) -> ComposeResult:
        yield Button("▶️ Запустить", id="btn-start", variant="success")
        yield Button("⏹ Остановить", id="btn-stop", variant="error")
        yield Button("🔄 Перезапустить", id="btn-restart", variant="warning")
        yield Button("🧪 Проверить БД", id="btn-dbcheck", variant="primary")
        yield Button("🚪 Выход", id="btn-quit", variant="default")


class LogsTab(Vertical):
    """Живые логи: RichLog + фильтр уровня + поиск."""

    LEVELS = ("ALL", "DEBUG", "INFO", "WARNING", "ERROR")

    def compose(self) -> ComposeResult:
        with Horizontal(id="log-filter-bar"):
            for lvl in self.LEVELS:
                yield Button(lvl, id=f"f-{lvl}",
                             variant="primary" if lvl == "ALL" else "default")
            yield Input(placeholder="🔎 фильтр строк…", id="log-search")
        yield RichLog(id="log-view", markup=True, wrap=True, highlight=True)

    def on_mount(self) -> None:
        self.filter_level = "ALL"
        self.search = ""
        # доедать буфер, накопленный до открытия вкладки
        for line in list(ui_log_handler.buffer)[-300:]:
            self._append(line)
        ui_log_handler.set_callback(self._ui_cb)
        self.set_interval(1.0, self._flush)

    def on_unmount(self) -> None:
        ui_log_handler.set_callback(None)

    # колбэк может прийти из event loop'а — очередь безопасна
    def _ui_cb(self, line: str) -> None:
        self._pending = getattr(self, "_pending", [])
        self._pending.append(line)

    def _flush(self) -> None:
        pending = getattr(self, "_pending", [])
        self._pending = []
        for line in pending:
            self._append(line)

    def _colored(self, line: str) -> str:
        if "| ERROR  " in line or "| CRITICAL" in line:
            return f"[bold red]{line}[/]"
        if "| WARNING" in line:
            return f"[yellow]{line}[/]"
        return f"[dim]{line}[/]" if "| DEBUG" in line else line

    def _passes(self, line: str) -> bool:
        lvl = self.filter_level
        if lvl != "ALL":
            # формат строки: "HH:MM:SS | LEVEL   | name - msg" (level padded до 7)
            parts = line.split("|", 2)
            actual = parts[1].strip() if len(parts) >= 2 else ""
            if actual != lvl:
                return False
        if self.search and self.search.lower() not in line.lower():
            return False
        return True

    def _append(self, line: str) -> None:
        if self._passes(line):
            self.query_one("#log-view", RichLog).write(self._colored(line))

    @on(Button.Pressed, "#f-ALL,#f-DEBUG,#f-INFO,#f-WARNING,#f-ERROR")
    def _set_filter(self, event: Button.Pressed) -> None:
        self.filter_level = event.button.id[2:]
        for lvl in self.LEVELS:
            btn = self.query_one(f"#f-{lvl}", Button)
            btn.variant = "primary" if lvl == self.filter_level else "default"
        self.refresh_log()

    @on(Input.Changed, "#log-search")
    def _set_search(self, event: Input.Changed) -> None:
        self.search = event.value
        self.refresh_log()

    def refresh_log(self) -> None:
        view = self.query_one("#log-view", RichLog)
        view.clear()
        for line in list(ui_log_handler.buffer)[-500:]:
            self._append(line)


class DashboardTab(Vertical):
    """Панель статуса + задачи планировщика + сводка БД."""

    def compose(self) -> ComposeResult:
        yield Static("[b]Состояние[/b]", id="dash-state")
        yield Static("[b]Задачи планировщика:[/b]\n—", id="dash-jobs")
        yield Static("[b]Сводка БД:[/b]\n—", id="dash-db")
        with Horizontal():
            yield Button("🔁 Обновить", id="dash-refresh", variant="primary")

    def on_mount(self) -> None:
        self.set_interval(5.0, self._refresh_async)
        self._refresh_async()

    @on(Button.Pressed, "#dash-refresh")
    def _btn_refresh(self) -> None:
        self._refresh_async()

    def _refresh_async(self) -> None:
        self.refresh_dashboard()

    @work(thread=False, exclusive=True)
    async def refresh_dashboard(self) -> None:
        label, _ = STATE_LABELS.get(runtime.state, ("❓", "white"))
        lines = [
            f"Статус: {label}",
            f"Uptime: {fmt_uptime(runtime.uptime_sec)}" if runtime.state == "running" else "Uptime: —",
        ]
        if runtime.last_error:
            lines.append(f"[red]Последняя ошибка: {runtime.last_error}[/red]")
        jobs = runtime.jobs_info()
        jobs_txt = "\n".join(f"  • {jid}: следующий запуск {nxt}" for jid, nxt in jobs) \
            or "  (бот остановлен)"
        db_txt = await self._db_summary()
        try:
            self.query_one("#dash-state", Static).update("\n".join(lines))
            self.query_one("#dash-jobs", Static).update(
                "[b]Задачи планировщика:[/b]\n" + jobs_txt)
            self.query_one("#dash-db", Static).update("[b]Сводка БД:[/b]\n" + db_txt)
        except Exception:  # noqa: BLE001 — виджет мог быть перемонтирован
            pass

    async def _db_summary(self) -> str:
        try:
            async with session_factory() as s:
                users = await s.scalar(select(func.count(User.tg_id)))
                pets = await s.scalar(select(func.count(Pet.id)))
                msgs = await s.scalar(select(func.count(ChatMessageLog.id)))
                ach = await s.scalar(select(func.count(Achievement.id)))
                unsent = await s.scalar(
                    select(func.count(NotificationQueue.id)).where(
                        NotificationQueue.sent.is_(False)))
            return (f"  пользователей: {users or 0}\n  питомцев: {pets or 0}\n"
                    f"  сообщений в логе: {msgs or 0}\n  ачивок (справочник): {ach or 0}\n"
                    f"  не отправленных уведомлений: {unsent or 0}")
        except Exception as exc:  # noqa: BLE001
            return f"  [red]БД недоступна: {str(exc)[:120]}[/red]"


TABLES = {
    "users": User,
    "pets": Pet,
    "chat_messages_log": ChatMessageLog,
    "achievements": Achievement,
    "notification_queue": NotificationQueue,
}


class DatabaseTab(Vertical):
    """Просмотр таблиц БД (read-only, LIMIT 200)."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="db-toolbar"):
            for name in TABLES:
                yield Button(name, id=f"t-{name}",
                             variant="primary" if name == "users" else "default")
        yield DataTable(id="db-table", zebra_stripes=True)
        yield Static("", id="db-status")

    def on_mount(self) -> None:
        self._load("users")

    @on(Button.Pressed, "#t-users,#t-pets,#t-chat_messages_log,#t-achievements,#t-notification_queue")
    def _pick(self, event: Button.Pressed) -> None:
        for name in TABLES:
            btn = self.query_one(f"#t-{name}", Button)
            btn.variant = "primary" if f"t-{name}" == event.button.id else "default"
        self._load(event.button.id[2:])

    @work(exclusive=True)
    async def _load(self, table: str) -> None:
        model = TABLES[table]
        status = self.query_one("#db-status", Static)
        dt = self.query_one("#db-table", DataTable)
        status.update(f"загрузка {table}…")
        try:
            async with session_factory() as s:
                rows = (await s.execute(select(model).limit(200))).scalars().all()
            cols = [c.name for c in model.__table__.columns]
            dt.clear(columns=True)
            dt.add_columns(*[(c, c) for c in cols])
            for obj in rows:
                vals = []
                for c in cols:
                    v = getattr(obj, c, None)
                    sv = str(v) if v is not None else ""
                    vals.append(sv[:40])
                dt.add_row(*vals)
            status.update(f"{table}: показано {len(rows)} строк (макс. 200)")
        except Exception as exc:  # noqa: BLE001
            status.update(f"[red]ошибка: {str(exc)[:160]}[/red]")


class SettingsTab(Vertical):
    """Редактор .env: таблица ключей, Enter/click — модалка, затем запись."""

    def compose(self) -> ComposeResult:
        yield Static("[b]Настройки (.env)[/b] — изменения применяются кнопкой "
                     "«Применить»; часть требует рестарта бота.", id="set-help")
        yield DataTable(id="set-table")
        with Horizontal(id="set-toolbar"):
            yield Button("💾 Применить (сохранить .env)", id="set-save", variant="success")
            yield Button("🔄 Сохранить и перезапустить бота", id="set-restart", variant="warning")
            yield Button("↻ Отменить несохранённое", id="set-discard", variant="default")
        yield Static("", id="set-status")

    def on_mount(self) -> None:
        self._pending: dict[str, str] = {}
        self._reload_table()

    def _display_value(self, key: str, typ: str, raw: str) -> str:
        if key in settings_io.SECRET_KEYS:
            return settings_io.mask_secret(raw)
        return raw

    def _reload_table(self) -> None:
        env = settings_io.load_env()
        dt = self.query_one("#set-table", DataTable)
        dt.clear(columns=True)
        dt.add_columns(("Настройка", "title"), ("Значение", "value"),
                       ("Ключ", "key"))
        for key, title, typ, _hint in settings_io.SETTINGS_SPEC:
            raw = env.get(key, "")
            shown = self._display_value(key, typ, raw)
            if key in self._pending:
                shown = self._display_value(key, typ, self._pending[key]) + "  ✏️"
            dt.add_row(title, shown, key, key=key)

    @on(DataTable.CellSelected, "#set-table")
    def _edit_cell(self, event: DataTable.CellSelected) -> None:
        dt = self.query_one("#set-table", DataTable)
        row = dt.get_row(event.coordinate.row)
        key = str(row[2])
        spec = next(s for s in settings_io.SETTINGS_SPEC if s[0] == key)
        current = self._pending.get(key, settings_io.load_env().get(key, ""))
        self.app.push_screen(
            EditSetting(spec[0], spec[1], spec[2], current, spec[3]),
            lambda new_value, k=key: self._apply_edit(k, new_value))

    def _apply_edit(self, key: str, new_value: str | None) -> None:
        if new_value is None:
            return
        self._pending[key] = new_value
        self._reload_table()
        self.query_one("#set-status", Static).update(
            f"[yellow]изменено {key}, не сохранено ({len(self._pending)})[/yellow]")

    def _save_pending(self) -> bool:
        if not self._pending:
            self.query_one("#set-status", Static).update("нечего сохранять")
            return False
        updates = dict(self._pending)
        try:
            settings_io.save_env(updates)
            settings_io.apply_to_runtime(updates)
            get_settings.cache_clear()
            self._pending.clear()
            self._reload_table()
            self.query_one("#set-status", Static).update(
                f"[green]✅ .env обновлён ({len(updates)} ключей), "
                "настройки применены к процессу[/green]")
            logger.info("оболочка: .env обновлён ({} ключей)", len(updates))
            return True
        except Exception as exc:  # noqa: BLE001
            self.query_one("#set-status", Static).update(f"[red]ошибка записи: {exc}[/red]")
            return False

    @on(Button.Pressed, "#set-save")
    def _save(self) -> None:
        self._save_pending()

    @on(Button.Pressed, "#set-discard")
    def _discard(self) -> None:
        self._pending.clear()
        self._reload_table()
        self.query_one("#set-status", Static).update("несохранённые изменения отменены")

    @on(Button.Pressed, "#set-restart")
    def _restart(self) -> None:
        if self._save_pending():
            self.app.restart_bot()


class SearchTab(Vertical):
    """Поиск пользователя по ID или части ника/имени."""

    def compose(self) -> ComposeResult:
        yield Input(placeholder="tg_id или часть username / имени…", id="u-search")
        yield Static("", id="u-result")

    @on(Input.Submitted, "#u-search")
    def _search(self, event: Input.Submitted) -> None:
        self._run(event.value.strip())

    @work(exclusive=True)
    async def _run(self, query: str) -> None:
        out = self.query_one("#u-result", Static)
        if not query:
            out.update("")
            return
        try:
            async with session_factory() as s:
                if query.lstrip("-").isdigit():
                    res = await s.execute(select(User).where(User.tg_id == int(query)))
                else:
                    like = f"%{query}%"
                    res = await s.execute(
                        select(User).where(
                            (User.username.ilike(like)) | (User.first_name.ilike(like))
                        ).limit(20))
                users = res.scalars().all()
            if not users:
                out.update("ничего не найдено")
                return
            lines = []
            for u in users:
                pet = getattr(u, "pet", None)
                pet_s = f"🐾 {pet.name} (ур.{pet.level})" if pet else "без питомца"
                lines.append(
                    f"[b]{u.first_name}[/b] @{u.username or '—'} · id={u.tg_id} · "
                    f"L{u.level} xp={u.xp} 🪙{u.coins} streak={u.streak_days} · {pet_s}"
                    + (" · [red]BAN[/red]" if u.is_banned else ""))
            out.update("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            out.update(f"[red]ошибка: {str(exc)[:160]}[/red]")


# ---------------------------------------------------------------------- app
class TamaConsoleApp(App):
    """Главное приложение оболочки."""

    TITLE = "TamaConsole — управление ботом"
    CSS_PATH = "tamaconsole.tcss"
    BINDINGS = [
        ("q", "quit_request", "Выход"),
        ("r", "toggle_run", "Пуск/стоп"),
        ("d", "focus_db", "БД"),
        ("l", "focus_logs", "Логи"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield StatusPanel(id="status-panel")
        yield ControlsBar(id="controls")
        with TabbedContent(initial="tab-logs"):
            with TabPane("📜 Логи", id="tab-logs"):
                yield LogsTab(id="pane-logs")
            with TabPane("📊 Дашборд", id="tab-dash"):
                yield DashboardTab(id="pane-dash")
            with TabPane("🗄 База данных", id="tab-db"):
                yield DatabaseTab(id="pane-db")
            with TabPane("⚙️ Настройки", id="tab-settings"):
                yield SettingsTab(id="pane-settings")
            with TabPane("🔎 Пользователи", id="tab-users"):
                yield SearchTab(id="pane-users")
        yield Footer()

    # ------------------------------------------------------------- lifecycle
    def on_mount(self) -> None:
        self._start_bot()  # запускает autostart как worker (run_action вернул бы coroutine)
        panel = self.query_one(StatusPanel)
        panel.refresh_now()
        self.set_interval(1.0, lambda: self.query_one(StatusPanel).refresh_now())
        self._update_buttons()

    @work(exclusive=True)
    async def _start_bot(self) -> None:
        """Автозапуск, если включён AUTO_START=true в .env/окружении."""
        env = settings_io.load_env()
        flag = env.get("AUTO_START", "").lower() in ("1", "true", "yes", "да")
        if flag:
            self.log("AUTO_START=true → запускаю бота")
            try:
                await runtime.start()
            except Exception as exc:  # noqa: BLE001
                logger.error("автозапуск не удался: {}", exc)
            finally:
                self._update_buttons()

    async def on_unmount(self) -> None:
        ui_log_handler.set_callback(None)
        if runtime.state in ("running", "starting"):
            await runtime.stop()

    # -------------------------------------------------------------- controls
    def _update_buttons(self) -> None:
        running = runtime.state == "running"
        starting = runtime.state == "starting"
        self.query_one("#btn-start", Button).disabled = running or starting
        self.query_one("#btn-stop", Button).disabled = not running and not starting
        self.query_one("#btn-restart", Button).disabled = starting

    @on(Button.Pressed, "#btn-start")
    def _start(self) -> None:
        self.start_bot()

    @on(Button.Pressed, "#btn-stop")
    def _stop(self) -> None:
        self.stop_bot()

    @on(Button.Pressed, "#btn-restart")
    def _restart_btn(self) -> None:
        self.restart_bot()

    @on(Button.Pressed, "#btn-dbcheck")
    def _dbcheck(self) -> None:
        self.check_database()

    @on(Button.Pressed, "#btn-quit")
    def _quit_btn(self) -> None:
        self.action_quit_request()

    @work(exclusive=True)
    async def start_bot(self) -> None:
        try:
            await runtime.start()
        except Exception as exc:  # noqa: BLE001
            logger.error("не удалось запустить бота: {}", exc)
        finally:
            self._update_buttons()

    @work(exclusive=True)
    async def stop_bot(self) -> None:
        try:
            await runtime.stop()
        except Exception as exc:  # noqa: BLE001
            logger.error("ошибка остановки: {}", exc)
        finally:
            self._update_buttons()

    @work(exclusive=True)
    async def restart_bot(self) -> None:
        logger.info("перезапуск бота по запросу оболочки…")
        try:
            await runtime.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("остановка при перезапуске: {}", exc)
        try:
            await runtime.start()
        except Exception as exc:  # noqa: BLE001
            logger.error("перезапуск не удался: {}", exc)
        finally:
            self._update_buttons()

    @work(exclusive=True)
    async def check_database(self) -> None:
        """Проверка доступности MySQL/Redis из панели управления."""
        from sqlalchemy import text
        from app.db.session import engine
        from app.utils.redis import redis_client
        results = []
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            results.append("MySQL: OK")
        except Exception as exc:  # noqa: BLE001
            results.append(f"MySQL: ошибка — {str(exc)[:100]}")
        try:
            if redis_client is not None:
                pong = await redis_client.ping()
                results.append(f"Redis: {'OK' if pong else 'FAIL'}")
            else:
                results.append("Redis: не инициализирован (in-memory fallback)")
        except Exception as exc:  # noqa: BLE001
            results.append(f"Redis: ошибка — {str(exc)[:100]}")
        msg = " · ".join(results)
        logger.info("проверка соединений: {}", msg)
        try:
            self.notify(msg, title="Проверка соединений", timeout=6)
        except Exception:  # noqa: BLE001
            pass

    # --------------------------------------------------------------- actions
    def action_toggle_run(self) -> None:
        if runtime.state == "running":
            self.stop_bot()
        elif runtime.state == "stopped":
            self.start_bot()

    def action_focus_db(self) -> None:
        self.query_one(TabbedContent).active = "tab-db"

    def action_focus_logs(self) -> None:
        self.query_one(TabbedContent).active = "tab-logs"

    def action_quit_request(self) -> None:
        def _done(confirmed: bool) -> None:
            if confirmed:
                self.exit()
        self.push_screen(ConfirmQuit(), _done)


def main() -> None:
    """Точка входа оболочки: python -m app.console"""
    setup_file_logging(get_settings().log_level)
    TamaConsoleApp().run()


if __name__ == "__main__":
    main()
