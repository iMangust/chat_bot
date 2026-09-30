from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

__version__ = "1.0.1"

_ENV_ENCODINGS = ("utf-8-sig", "cp1251", "latin-1")

_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")

def _strip_inline_comment(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    v = _INLINE_COMMENT_RE.sub("", v).strip()
    if v.startswith("#"):
        return ""
    return v

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
        return (init_settings, env_settings, sanitized_dotenv,
                file_secret_settings)

    bot_token: str = ""
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
    # Явный токен доступа к API панели (приоритет над WEBHOOK_SECRET_TOKEN/BOT_TOKEN)
    dashboard_token: str = ""

    tz_offset_hours: int = 12  # Камчатка (UTC+12); см. Asia/Kamchatka
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

    channel_username: str | None = None
    channel_username_visual: str | None = None
    channel_chat_id: int | None = None

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
                pass
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
            pass


def utc_hour_of(local_hour: int) -> int:
    """Локальный час (TZ_OFFSET_HOURS) -> час в UTC."""
    return (local_hour - get_settings().tz_offset_hours) % 24


def local_hour_of(utc_hour: int) -> int:
    """Час в UTC -> локальный час (TZ_OFFSET_HOURS)."""
    return (utc_hour + get_settings().tz_offset_hours) % 24
