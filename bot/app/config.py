from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource

__version__ = "1.0.0"

_ENV_ENCODINGS = ("utf-8-sig", "cp1251", "latin-1")

_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")

def _strip_inline_comment(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    v = _INLINE_COMMENT_RE.sub("", v).strip()
    if v.startswith("#"):
        return ""
    return v


def _normalize_int_list(v: object) -> object:
    """Приводит значение списка id к валидному JSON-массиву целых.

    Поддерживает форматы .env:
      TRACKED_CHAT_IDS=[-1004335857237,-1004467842206]   (JSON — нативно)
      TRACKED_CHAT_IDS=-1004335857237,-1004467842206     (CSV без скобок)
      TRACKED_CHAT_IDS="[-100..., -100...]"              (пробелы/кавычки)
      TRACKED_CHAT_IDS=[]                                (пусто)
    Пустые/нечисловые токены отбрасываются; если не осталось ни одного
    числа — возвращаем [], чтобы pydantic не падал на битом значении.
    """
    if v is None or isinstance(v, (list, tuple)):
        return v
    s = str(v).replace(";", ",")
    nums: list[int] = []
    for tok in s.split(","):
        # скобки/кавычки могли быть у каждого элемента: [-1001], [-1002]
        tok = tok.strip().strip("[]'\"").strip()
        if not tok:
            continue
        try:
            nums.append(int(tok))
        except ValueError:
            continue
    return nums

def _read_env_values(path: str) -> dict[str, str]:
    raw = Path(path).read_bytes()
    text = None
    for enc in _ENV_ENCODINGS:
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    vals: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip()
        if k.startswith("export "):
            k = k[len("export "):].strip()
        v = _strip_inline_comment(v.strip())
        if k:
            vals[k] = v
    return vals

_ENV_CANDIDATES = [
    p for p in (
        os.getenv("ENV_FILE"),
        str(Path(__file__).resolve().parent.parent / ".env"),
        str(Path(__file__).resolve().parent.parent.parent / ".env"),
        ".env",
    ) if p
]

def _env_files() -> tuple[str, ...]:
    return tuple(p for p in _ENV_CANDIDATES if Path(p).is_file()) or (".env",)

def _alias_short_mtproto_keys() -> None:
    vals: dict[str, str] = {}
    for path in _env_files():
        try:
            vals.update(_read_env_values(path))
        except (OSError, UnicodeError, ValueError):
            continue
    pairs = {
        "TELEGRAM_API_ID": ("API_ID",),
        # обратные алиасы: короткое имя берём из длинного, если задано только оно
        "TELEGRAM_API_HASH": ("API_HASH",),
        "TELEGRAM_PHONE": ("PHONE",),
        "TELEGRAM_PASSWORD": ("TELEGRAM_PASSWORD",),
        "MTPROTO_SESSION_STRING": ("SESSION_STRING",),
        "MTPROTO_SESSION": ("SESSION_FILE",),
        "MTPROTO_ANSWER_MODE": ("ANSWER_MODE",),
    }
    # обратный маппинг: если задано только длинное имя — проставляем короткое
    # (часть кода читает их напрямую через os.getenv)
    reverse = {
        "API_ID": ("TELEGRAM_API_ID",),
        "API_HASH": ("TELEGRAM_API_HASH",),
        "PHONE": ("TELEGRAM_PHONE",),
        "SESSION_STRING": ("MTPROTO_SESSION_STRING",),
        "SESSION_FILE": ("MTPROTO_SESSION",),
        "ANSWER_MODE": ("MTPROTO_ANSWER_MODE",),
    }
    for mapping in (pairs, reverse):
        for target, sources in mapping.items():
            if os.getenv(target):
                continue
            for src in sources:
                v = os.getenv(src) or vals.get(src)
                if v:
                    os.environ[target] = v
                    break

_alias_short_mtproto_keys()


class EnvListSafeSource(EnvSettingsSource):
    """Списки id из os.environ (TRACKED_CHAT_IDS/ADMIN_IDS) могут быть в CSV
    формате "a,b" — штатный источник парсит сложные поля строго как JSON и
    падает с SettingsError. Нормализуем через _normalize_int_list (поддержаны
    оба формата).

    Это же чинит hot-apply настроек из панели: write_env() пишет новые значения
    в .env И в os.environ, а кэшированный get_settings() после cache_clear()
    перечитывает конфигурацию именно отсюда — раньше CSV из os.environ ронял
    перезагрузку конфига, и новые списки чатов применялись только после
    рестарта процесса.
    """

    _LIST_KEYS = {"tracked_chat_ids", "admin_ids"}

    def prepare_field_value(self, field_name, field, value, value_is_complex):
        if field_name in self._LIST_KEYS and isinstance(value, str):
            return _normalize_int_list(_strip_inline_comment(value))
        return super().prepare_field_value(
            field_name, field, value, value_is_complex)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_files(), env_file_encoding="utf-8-sig", extra="ignore")

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings,
                                   env_settings, dotenv_settings,
                                   file_secret_settings):
        def sanitized_dotenv() -> dict[str, object]:
            out: dict[str, object] = {}
            for k, v in dotenv_settings().items():
                if isinstance(v, str):
                    v = _strip_inline_comment(v)
                    if v == "":
                        continue
                out[k] = v
            return out
        safe_env = EnvListSafeSource(settings_cls)
        return (init_settings, safe_env, sanitized_dotenv,
                file_secret_settings)

    bot_token: str = ""

    @field_validator("tracked_chat_ids", "admin_ids", mode="before")
    @classmethod
    def _ids_from_env(cls, v: object) -> object:
        # pydantic-settings умеет только JSON-массивы; пользовательский .env
        # мог быть в CSV без скобок — нормализуем оба формата.
        return _normalize_int_list(v)

    @model_validator(mode="after")
    def _reject_default_webhook_secret_in_prod(self):
        # Issue #8 аудита: секрет вебхука по умолчанию ("change-me-in-env")
        # известен из исходников. В webhook-режиме (не dev, задан WEBHOOK_URL)
        # Telegram шлёт его в заголовке X-Telegram-Bot-Api-Secret-Token, и
        # aiogram проверяет точное совпадение — дефолт = любой, кто знает
        # строку, может подделать апдейты. Не запускаемся с таким конфигом.
        if (not self.is_dev and self.webhook_url
                and self.webhook_secret_token == "change-me-in-env"):
            raise ValueError(
                "WEBHOOK_SECRET_TOKEN не настроен: в production (IS_DEV=false "
                "+ WEBHOOK_URL) требуется задать уникальный секрет вебхука, "
                "иначе апдейты Telegram можно подделать. Сгенерируйте, напр.: "
                "python -c \"import secrets; print(secrets.token_hex(32))\"")
        return self

    tracked_chat_ids: list[int] = []

    database_url: str = "mysql+aiomysql://tamabot:tamabot@127.0.0.1:3306/tamabot?charset=utf8mb4"
    redis_url: str = "redis://127.0.0.1:6379/0"

    polling_timeout: int = 10
    polling_limit: int = 50

    min_message_length: int = 5
    activity_cooldown_sec: int = 10
    reactions_cap_per_day: int = 20
    xp_per_message: int = 2
    xp_level_base: float = 50.0
    coins_per_message_cap: int = 1
    # Баланс: жёсткий дневной потолок монет за активность в чатах.
    # Без него 🪙 можно бесконечно фармить сообщениями (auto-post/флуд).
    coins_daily_cap: int = 25

    log_level: str = "INFO"
    is_dev: bool = True
    webhook_url: str | None = None
    webhook_port: int = 8081
    webhook_secret_token: str = "change-me-in-env"

    admin_ids: list[int] = []

    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    dashboard_allowed_ips: str = ""
    dashboard_trust_proxy: bool = False
    # Явный токен доступа к API панели (приоритет над WEBHOOK_SECRET_TOKEN;
    # BOT_TOKEN как токен панели больше не используется — issue #4 аудита)
    dashboard_token: str = ""

    tz_offset_hours: int = 0  # смещение локального времени от UTC (0 = UTC)
    # Именованная IANA-зона (например Europe/Berlin): включает честный DST
    # вместо жёсткого смещения. Пусто — работать по TZ_OFFSET_HOURS.
    tz_name: str = ""
    # Часы отправки в ЛОКАЛЬНОМ времени (TZ_OFFSET_HOURS), а не в UTC:
    # раньше назывались *_utc, но планировщик работал в MSK+9 — путаница.
    daily_report_hour: int = 21
    morning_reminder_hour: int = 9
    evening_reminder_hour: int = 20
    pet_warning_min_hours: int = 6
    streak_warn_threshold_sec: int = 6 * 3600
    invite_reward_coins: int = 50
    show_invite_button: bool = True
    weather_enabled: bool = True
    weather_real_enabled: bool = True
    weather_refresh_hours: float = 3.0
    weather_cache_minutes: int = 180
    openweather_api_token: str | None = None
    openweather_api_key: str | None = None
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telegram_phone: str | None = None
    mtproto_session_string: str | None = None
    mtproto_session: str = "mtproto_sync"
    mtproto_answer_mode: str = "off"

    weather_city: str = "Петропавловск-Камчатский"
    weather_lat: float = 53.0446
    weather_lon: float = 158.6507
    redis_socket_timeout: int = 5

    # Юзернейм бота (без @) — для deep-link кнопок вида
    # https://t.me/<bot>?start=<payload>. Если не задан — подтягивается
    # автоматически через get_me() при старте (см. main.py).
    bot_username: str | None = None

    channel_username: str | None = None
    channel_username_visual: str | None = None
    channel_chat_id: int | None = None
    # Группа обсуждений при канале. Не обязателен, если её id уже есть в
    # TRACKED_CHAT_IDS; если задан — автоматически добавляется в отслеживаемые.
    chat_discussion_group: int | None = None

    channel_scan_minutes: int = 30

    merch_admin_id: int | None = None

    telegram_password: str | None = None
    mtproto_autosync: bool = True
    mtproto_sync_minutes: int = 60
    mtproto_full_scan_hours: int = 6

    merch_enabled: bool = False
    merch_url: str | None = None
    merch_items: str | None = None

@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # Обратная совместимость старых .env: раньше часы рассылок задавались в UTC
    # (DAILY_REPORT_HOUR_UTC и т.п.), теперь — в локальном времени. Если в окружении
    # остались только старые ключи, пересчитываем их в локальные часы.
    _legacy_map = {
        "daily_report_hour": "DAILY_REPORT_HOUR_UTC",
        "morning_reminder_hour": "MORNING_REMINDER_HOUR_UTC",
        "evening_reminder_hour": "EVENING_REMINDER_HOUR_UTC",
    }
    for field, env_key in _legacy_map.items():
        raw = os.environ.get(env_key)
        if raw is None or not str(raw).strip():
            continue
        new_key = field.upper()
        if os.environ.get(new_key) is not None:  # явное новое значение важнее
            continue
        try:
            legacy_utc = int(str(raw))
        except ValueError:
            continue
        object.__setattr__(s, field, (legacy_utc + s.tz_offset_hours) % 24)
    # Pydantic Settings по умолчанию не читает переменные окружения в момент
    # вызова — фиксируем часы рассылок явно, чтобы их можно было менять
    # через панель без рестарта (HOT_KEYS).
    for field in _legacy_map:
        env_val = os.environ.get(field.upper())
        if env_val is not None and str(env_val).strip():
            try:
                object.__setattr__(s, field, int(str(env_val)))
            except ValueError:
                print(f"[config] WARNING: {field.upper()}={env_val!r} — "
                      f"не целое число, значение проигнорировано")
    # CHAT_DISCUSSION_GROUP — не дублирование TRACKED_CHAT_IDS, а опциональная
    # подсказка (например, группа обсуждений при канале). Если id задан и его
    # ещё нет в списке отслеживаемых — добавляем автоматически, чтобы учёт
    # статистики/реакций работал без ручной синхронизации двух переменных.
    if s.chat_discussion_group is not None:
        merged = list(s.tracked_chat_ids or [])
        if int(s.chat_discussion_group) not in [int(x) for x in merged]:
            merged.append(int(s.chat_discussion_group))
            object.__setattr__(s, "tracked_chat_ids", merged)
    # PANEL-настройки (списки id) пишутся в .env и os.environ; перечитка
    # через EnvListSafeSource уже обработана выше. Здесь синхронизируем
    # CHAT_DISCUSSION_GROUP поверх значения из os.environ (см. комментарий
    # над предыдущим блоком — merge нужен и для hot-apply тоже).
    if s.chat_discussion_group is not None and os.environ.get("TRACKED_CHAT_IDS"):
        merged = list(s.tracked_chat_ids or [])
        if int(s.chat_discussion_group) not in [int(x) for x in merged]:
            merged.append(int(s.chat_discussion_group))
            object.__setattr__(s, "tracked_chat_ids", merged)
    return s


def apply_hot_schedule_keys(updates: dict[str, str]) -> None:
    """Применяет DAILY_REPORT_HOUR / MORNING|EVENING_REMINDER_HOUR из сохранённых
    настроек к живому экземпляру Settings (без рестарта процесса)."""
    s = get_settings()
    mapping = {"DAILY_REPORT_HOUR": "daily_report_hour",
               "MORNING_REMINDER_HOUR": "morning_reminder_hour",
               "EVENING_REMINDER_HOUR": "evening_reminder_hour"}
    for key, field in mapping.items():
        val = updates.get(key)
        if val is None or not str(val).strip():
            continue
        try:
            object.__setattr__(s, field, int(str(val)))
        except ValueError:
            print(f"[config] WARNING: hot key {key}={val!r} — "
                  f"не целое число, настройка не применена")


def utc_hour_of(local_hour: int) -> int:
    """Локальный час (TZ_OFFSET_HOURS) -> час в UTC."""
    return (local_hour - get_settings().tz_offset_hours) % 24


def local_hour_of(utc_hour: int) -> int:
    """Час в UTC -> локальный час (TZ_OFFSET_HOURS)."""
    return (utc_hour + get_settings().tz_offset_hours) % 24
