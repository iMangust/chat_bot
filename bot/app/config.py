"""Конфигурация приложения.

Все секреты только из окружения (.env). Никаких хардкод-токенов.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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
    polling_timeout: int = 30          # long polling timeout, сек
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

    # Администраторы бота (tg_id), через запятую в env: ADMIN_IDS=1,2
    admin_ids: list[int] = []


@lru_cache
def get_settings() -> Settings:
    return Settings()
