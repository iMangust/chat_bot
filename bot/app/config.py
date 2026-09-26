"""Конфигурация приложения.

Все секреты только из окружения (.env). Никаких хардкод-токенов.

v1.5.12: .env ищется по абсолютным путям (бот можно запускать из любой
директории), плюс поддерживаются «короткие» имена ключей MTProto из
.env.example (API_ID / API_HASH / PHONE) — раньше они молча игнорировались,
и синхронизация «не происходила» при полностью раскомментированном блоке.

v1.5.13: фикс падения на Windows (TypeError в dotenv: файл передавался
объектом вместо пути) + устойчивое чтение .env.

v1.5.14: фикс UnicodeDecodeError на Windows-1251/BOM: .env читается с
автоподбором кодировки (utf-8-sig → cp1251 → latin-1); ошибки парсинга
больше не роняют импорт конфига.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

__version__ = "1.5.15"

# Кодировки для чтения .env: BOM-aware utf-8 → кириллица Windows → всегда
# успешная latin-1 (последняя гарантирует, что декодирование не упадёт).
_ENV_ENCODINGS = ("utf-8-sig", "cp1251", "latin-1")


def _read_env_values(path: str) -> dict[str, str]:
    """Прочитать пары KEY=VALUE из .env без жёсткой привязки к кодировке.

    Исторические падения на Windows (v1.5.13/1.5.14):
      - dotenv_values(TextIOWrapper) — TypeError (новые версии принимают
        только путь/строку);
      - UTF-8-файл с кириллицей в системной cp1251 — UnicodeDecodeError.
    Поэтому читаем байты сами, подбираем кодировку (utf-8-sig → cp1251 →
    latin-1) и парсим простым KEY=VALUE без python-dotenv.
    """
    raw = Path(path).read_bytes()
    text = None
    for enc in _ENV_ENCODINGS:
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:  # недостижимо (latin-1 не падает), но на всякий случай
        text = raw.decode("utf-8", errors="replace")
    # Нормализуем переносы строк (Windows CRLF) и убираем BOM, если он
    # оказался в начале файла.
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
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k:
            vals[k] = v
    return vals

# Порядок поиска .env: переменная окружения ENV_FILE → корень проекта
# (рядом с этим файлом: bot/app/config.py → bot/.env) → репозиторий → cwd.
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
    """API_ID/API_HASH/PHONE (как в .env.example) == TELEGRAM_API_ID/.../PHONE.

    Pydantic читает только точные имена полей; чтобы блок вида
        API_ID=12345678
    работал без префикса, прокидываем значения в os.environ ДО создания
    Settings (реальные переменные окружения имеют приоритет над .env, но
    если их нет — алиасы подхватятся). Ничего не логируем: ключи секретны.
    """
    vals: dict[str, str] = {}
    for path in _env_files():
        try:
            vals.update(_read_env_values(path))
        except (OSError, UnicodeError, ValueError):
            # гонка/права/битый файл — не роняем импорт конфига;
            # дальше сработают обычные переменные окружения
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

    # --- Telegram ---
    bot_token: str = ""
    # ID канала/группы, где бот считает активность (пусто = считать во всех чатах,
    # где бот состоит, кроме ЛС)
    tracked_chat_ids: list[int] = []

    # --- БД / Redis ---
    # MySQL (prod, Windows Server): mysql+aiomysql://user:pass@127.0.0.1:3306/tamabot?charset=utf8mb4
    # Fallback-драйвер без Rust-зависимостей: mysql+asyncmy://... или mysql+pymysql://... (sync)
    database_url: str = "mysql+aiomysql://tamabot:tamabot@127.0.0.1:3306/tamabot?charset=utf8mb4"
    # Redis НЕ обязателен на Windows: есть in-memory fallback (кулдауны/FSM),
    # но для стабильности рекомендуется Memurai / WSL2-Redis.
    redis_url: str = "redis://127.0.0.1:6379/0"

    # --- Способ запуска (Windows Server: только long polling) ---
    polling_timeout: int = 10          # long polling timeout, сек (getUpdates)
    polling_limit: int = 50            # max updates за один запрос

    # --- Антифрод / баланс ---
    min_message_length: int = 5          # минимальная длина сообщения для зачёта
    activity_cooldown_sec: int = 10      # кулдаун между зачётами сообщений
    reactions_cap_per_day: int = 20      # антифрод: максимум засчитанных реакций от A к B в сутки
    xp_per_message: int = 2              # базовый XP за засчитанное сообщение
    xp_level_base: float = 50.0          # формула уровня: xp_needed = base * level^1.5
    coins_per_message_cap: int = 1       # монеты за сообщение (с учётом кулдауна)

    # --- Прочее ---
    log_level: str = "INFO"
    is_dev: bool = True                  # poling vs webhook, отладочные хендлеры
    webhook_url: str | None = None
    webhook_port: int = 8081
    # Секретный токен вебхука (X-Telegram-Bot-Api-Secret-Token). В проде обязателен:
    # без него на ваш URL сможет слать фейковые апдейты кто угодно.
    webhook_secret_token: str = "change-me-in-env"

    # Администраторы бота (tg_id), через запятую в env: ADMIN_IDS=1,2
    admin_ids: list[int] = []

    # --- Уведомления / вовлечение ---
    tz_offset_hours: int = 3             # смещение МСК для «ночной совы» и праздников
    daily_report_hour_utc: int = 17      # ежедневный отчёт (17 UTC ≈ 20 МСК)
    morning_reminder_hour_utc: int = 7   # утреннее напоминание (7 UTC ≈ 10 МСК)
    evening_reminder_hour_utc: int = 16  # вечерний дайджест/«строк под угрозой» (16 UTC ≈ 19 МСК)
    pet_warning_min_hours: int = 6       # не чаще одного «питомец скучает» в N часов
    streak_warn_threshold_sec: int = 6 * 3600  # предупреждать о стрике за 6 ч до полуночи
    invite_reward_coins: int = 50        # награда пригласившему за друга
    weather_enabled: bool = True         # сезонная модификация деградации
    redis_socket_timeout: int = 5      # таймауты Redis (socket/connect), сек

    # --- Рефералка / канал ---
    # Публичный юзернейм КАНАЛА (без @). Реферальная ссылка ведёт на канал,
    # а не в группу: https://t.me/<channel_username>?start=invite_<tg_id>
    channel_username: str | None = None
    channel_chat_id: int | None = None   # ID канала (-100...), если бот там админ

    # --- Приветствие новичков канала ---
    # События chat_member приходят только если бот — админ канала с правом
    # «Manage users» и в allowed_updates есть "chat_member".
    welcome_channel_enabled: bool = True     # слать приветствие новым подписчикам канала
    channel_welcome_text: str | None = None  # свой текст ({name}, {channel}); пусто = дефолтный
    channel_scan_minutes: int = 30           # период фонового скана счётчика участников

    # --- Опционально: Telegram API (MTProto/Telethon) — sync подписчиков ---
    # Нужен ТОЛЬКО для разовой/периодической синхронизации списка участников
    # канала в welcome-очередь (python -m app.services.mtproto_sync).
    # Основной функционал бота работает без этих ключей (Bot API).
    # Регистрация ключей: https://my.telegram.org -> API development tools.
    # ВАЖНО: использовать ВТОРОЙ (service) аккаунт, не личный; секреты —
    # только в .env (файл в .gitignore), никогда в коде и репозитории.
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telegram_phone: str | None = None          # нужен только при интерактивном логине
    telegram_password: str | None = None       # 2FA облачный пароль (если есть)
    # Готовая строка сессии (без файла, для сервера): см. README «MTProto»
    mtproto_session_string: str | None = None
    mtproto_session: str = "mtproto_sync"      # либо путь к файлу .session
    # Режим «полного API» для входящих сообщений (v1.5.12):
    #   off     — только Bot API (дефолт; Telethon нужен лишь для sync);
    #   user    — отвечать в чатах от user-аккаунта (UserBot, требует
    #             интерактивного логина или готовой сессии);
    #   hybrid  — события/команды обрабатывает бот, UserBot дублирует
    #             ответы там, где бот не может (например, без админ-прав).
    mtproto_answer_mode: str = "off"
    # Автозапуск полной синхронизации участников при старте бота
    # (если заданы api_id/api_hash + сессия). Прогоняется один раз за старт.
    mtproto_autosync: bool = True
    # Период дельта-синхронизации через MTProto, минут (0 = выкл).
    mtproto_sync_minutes: int = 60

    # --- Мерч канала (отдельный раздел 🧢, не связан с питомцем) ---
    merch_enabled: bool = True           # показывать раздел 🧢 Мерч в главном меню
    merch_url: str | None = None         # внешняя ссылка на магазин мерча (кнопка «🌐 Открыть»)
    # Витрина: "Категория|Название|Цена₽|Описание|Размеры(S,M,L);..."
    # Категории: tshirt (футболки), hoodie (худи), acc (аксессуары). Пусто = дефолтная витрина.
    merch_items: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
