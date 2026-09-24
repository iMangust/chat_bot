"""Настройки оболочки TamaConsole: чтение/запись .env с сохранением комментариев.

Формат .env — простой KEY=VALUE, поэтому пишем аккуратно: существующие строки
обновляем on-place (комментарии и порядок сохраняются), новые ключи дописываем
в конец. Значения не кавычим (pydantic-settings это допускает) и экранируем
переводы строк.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import dotenv_values

_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def env_path() -> Path:
    """Путь к .env: переменная окружения TAMABOT_ENV_FILE или ./env рядом с кодом."""
    p = os.environ.get("TAMABOT_ENV_FILE", ".env")
    return Path(p)


def load_env() -> dict[str, str]:
    """Текущие значения из файла .env (без подстановки из окружения)."""
    raw = dotenv_values(env_path())
    return {k: (v or "") for k, v in raw.items() if k}


def save_env(updates: dict[str, str]) -> None:
    """Обновить/добавить ключи в .env, сохранив комментарии и порядок строк."""
    path = env_path()
    lines: list[str] = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)

    def encode(value: str) -> str:
        return value.replace("\\", "\\\\").replace("\n", "\\n")

    for i, line in enumerate(lines):
        m = _LINE_RE.match(line)
        if not m:
            continue
        key = m.group(1)
        if key in remaining:
            lines[i] = f"{key}={encode(remaining.pop(key))}"
    for key, value in remaining.items():
        lines.append(f"{key}={encode(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_to_runtime(updates: dict[str, str]) -> None:
    """Записать изменения также в os.environ (чтобы get_settings() видел их без рестарта
    процесса после cache_clear)."""
    for key, value in updates.items():
        os.environ[key.upper()] = value


# ---------------------------------------------------------------- keys meta
# (ключ, русское имя, тип, подсказка). Тип: str | int | bool | list[int] | secret
SETTINGS_SPEC: list[tuple[str, str, str, str]] = [
    ("BOT_TOKEN", "Токен бота", "secret", "от @BotFather"),
    ("DATABASE_URL", "Строка подключения MySQL", "str",
     "mysql+aiomysql://user:pass@127.0.0.1:3306/tamabot?charset=utf8mb4"),
    ("REDIS_URL", "Redis (опционально)", "str", "redis://127.0.0.1:6379/0"),
    ("TRACKED_CHAT_IDS", "ID канала/групп", "list[int]",
     "пусто = все чаты, где бот админ; пример: -100123,-100456"),
    ("ADMIN_IDS", "TG ID админов бота", "list[int]", "через запятую: 111,222"),
    ("MIN_MESSAGE_LENGTH", "Мин. длина сообщения для зачёта", "int", "символов"),
    ("ACTIVITY_COOLDOWN_SEC", "Кулдаун между зачётами", "int", "секунд"),
    ("REACTIONS_CAP_PER_DAY", "Макс. засчит. реакций A→B в сутки", "int", "штук"),
    ("XP_PER_MESSAGE", "XP за сообщение", "int", ""),
    ("XP_LEVEL_BASE", "База формулы уровня", "int", "xp_needed = base * L^1.5"),
    ("COINS_PER_MESSAGE_CAP", "Монеты за сообщение", "int", ""),
    ("POLLING_TIMEOUT", "Long polling timeout", "int", "секунд"),
    ("LOG_LEVEL", "Уровень логов", "str", "DEBUG / INFO / WARNING"),
    ("IS_DEV", "Режим разработки", "bool", ""),
    ("TZ_OFFSET_HOURS", "Часовой пояс (смещение, чч)", "int", "3 = МСК"),
    ("DAILY_REPORT_HOUR_UTC", "Час ежедневного отчёта (UTC)", "int", "17 ≈ 20 МСК"),
    ("MORNING_REMINDER_HOUR_UTC", "Утреннее напоминание (UTC)", "int", "7 ≈ 10 МСК"),
    ("EVENING_REMINDER_HOUR_UTC", "Вечерний дайджест (UTC)", "int", "16 ≈ 19 МСК"),
    ("PET_WARNING_MIN_HOURS", "«Питомец скучает» не чаще, ч", "int", ""),
    ("STREAK_WARN_THRESHOLD_SEC", "Порог предупреждения о стрике", "int", "секунд"),
    ("INVITE_REWARD_COINS", "Награда за приглашённого друга", "int", "монет"),
    ("WEATHER_ENABLED", "Сезоны влияют на деградацию", "bool", ""),
]

SECRET_KEYS = {k for k, _, t, _ in SETTINGS_SPEC if t == "secret"}


def mask_secret(value: str) -> str:
    """Для отображения секрета: первые 6 и последние 4 символа."""
    if not value:
        return ""
    if len(value) <= 12:
        return "•" * len(value)
    return f"{value[:6]}…{value[-4:]}"


def parse_value(raw: str, typ: str):
    """Разобрать значение из строки ввода по типу spec."""
    raw = raw.strip()
    if typ == "bool":
        low = raw.lower()
        if low in ("1", "true", "да", "yes", "on", "y"):
            return True
        if low in ("0", "false", "нет", "no", "off", "n"):
            return False
        raise ValueError("ожидается да/нет (true/false)")
    if typ == "int":
        return int(raw)
    if typ == "list[int]":
        items = [x.strip() for x in raw.split(",") if x.strip()]
        return [int(x) for x in items]
    return raw


def format_value(value, typ: str) -> str:
    """Значение python → строка для .env."""
    if value is None:
        return ""
    if typ == "bool":
        return "true" if value else "false"
    if typ == "list[int]":
        if isinstance(value, (list, tuple)):
            return ",".join(str(v) for v in value)
        return str(value)
    return str(value)
