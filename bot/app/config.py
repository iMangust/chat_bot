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
        "TELEGRAM_API_HASH": ("API_HASH",),
        "TELEGRAM_PHONE": ("PHONE",),
        "TELEGRAM_PASSWORD": ("TELEGRAM_PASSWORD",),
        "MTPROTO_SESSION_STRING": ("SESSION_STRING",),
        "MTPROTO_SESSION": ("SESSION_FILE",),
        "MTPROTO_ANSWER_MODE": ("ANSWER_MODE",),
    }
    for target, sources in pairs.items():
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

    dashboard_allowed_ips: str = ""
    dashboard_trust_proxy: bool = False

    tz_offset_hours: int = 3
    daily_report_hour_utc: int = 17
    morning_reminder_hour_utc: int = 7
    evening_reminder_hour_utc: int = 16
    pet_warning_min_hours: int = 6
    streak_warn_threshold_sec: int = 6 * 3600
    invite_reward_coins: int = 50
    weather_enabled: bool = True
    weather_real_enabled: bool = True
    weather_refresh_hours: float = 3.0
    weather_cache_minutes: int = 180
    openweather_api_token: str | None = None
    redis_socket_timeout: int = 5

    channel_username: str | None = None
    channel_chat_id: int | None = None

    channel_scan_minutes: int = 30

    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telegram_phone: str | None = None
    telegram_password: str | None = None
    mtproto_session_string: str | None = None
    mtproto_session: str = "mtproto_sync"
    mtproto_answer_mode: str = "off"
    mtproto_autosync: bool = True
    mtproto_sync_minutes: int = 60
    mtproto_full_scan_hours: int = 6

    merch_enabled: bool = False
    merch_url: str | None = None
    merch_items: str | None = None

@lru_cache
def get_settings() -> Settings:
    return Settings()
