"""Единый MTProto-клиент (Telethon) для всего «полного Telegram API».

v1.5.12. Назначение:
* один переиспользуемый клиент на процесс (подключение + keepalive);
* конфигурирование из Settings (креды приходят ТОЛЬКО из .env / окружения —
  в коде никаких реальных ключей, см. .env.example с серыми примерами);
* ленивая инициализация: без api_id/api_hash модуль импортопригоден, а
  вызовы дают понятную ошибку вместо падения бота;
* режимы стринг-сессии Telethon ('BQ...') и файла *.session.

Используется:
* app.services.mtproto_sync  — чтение списка участников канала (get_full_channel);
* app.services.userbot       — ответы от user-аккаунта (режимы user/hybrid);
* app.handlers.admin         — команды /mtproto, /syncnow.

ВАЖНО: это user-аккаунт. Флуд/рассылки с него запрещены правилами Telegram;
используйте второй (service) аккаунт, не личный.
"""
from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from app.config import get_settings

_TELETHON_IMPORT_ERROR: str | None = None


def telethon_available() -> bool:
    global _TELETHON_IMPORT_ERROR
    if _TELETHON_IMPORT_ERROR is not None:
        return _TELETHON_IMPORT_ERROR == ""
    try:
        import telethon  # noqa: F401
        _TELETHON_IMPORT_ERROR = ""
        return True
    except ImportError:
        _TELETHON_IMPORT_ERROR = "no"
        return False


def credentials_configured() -> bool:
    """Есть ли api_id+api_hash (минимум для подключения при готовой сессии)."""
    st = get_settings()
    return bool(st.telegram_api_id and st.telegram_api_hash)


def session_configured() -> bool:
    """Есть ли чем авторизоваться: строковая сессия или существующий файл."""
    from pathlib import Path
    st = get_settings()
    if st.mtproto_session_string:
        return True
    p = Path(st.mtproto_session or "")
    return p.with_suffix(".session").is_file() or p.is_file()


class MtprotoClientHolder:
    """Ленинный синглтон Telethon-клиента."""

    def __init__(self) -> None:
        self._client: Any | None = None
        self._lock = asyncio.Lock()
        self._me: Any | None = None

    async def _authorized(self) -> bool:
        """Telethon: is_user_authorized() — КОРУТИНА (await обязателен).

        Синхронный вызов давал RuntimeWarning «coroutine was never awaited»
        и всегда True (bool coroutine-объекта), из-за чего использовался
        отключённый клиент. v1.5.15: проверяем is_connected() синхронно,
        авторизацию — только через await.
        """
        c = self._client
        if c is None or not getattr(c, "is_connected", lambda: False)():
            return False
        try:
            return bool(await c.is_user_authorized())
        except Exception:  # noqa: BLE001
            return False

    async def get(self) -> Any:
        """Подключённый клиент либо RuntimeError с внятной подсказкой."""
        if await self._authorized():
            return self._client
        async with self._lock:
            if await self._authorized():
                return self._client
            if not telethon_available():
                raise RuntimeError(
                    "telethon не установлен: pip install 'telethon>=1.36'")
            st = get_settings()
            if not credentials_configured():
                raise RuntimeError(
                    "MTProto не настроен: укажите в .env TELEGRAM_API_ID "
                    "(или API_ID) и TELEGRAM_API_HASH (API_HASH) — "
                    "https://my.telegram.org → API development tools")
            from telethon import TelegramClient
            # session_string приоритетнее файла; '' невалиден — не передаём
            session = st.mtproto_session_string or st.mtproto_session or "mtproto_sync"
            client = TelegramClient(session, st.telegram_api_id,
                                    st.telegram_api_hash)
            if st.mtproto_session_string:
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    raise RuntimeError(
                        "MTPROTO_SESSION_STRING неавторизованна/истекла — "
                        "перегенерируйте: python -m app.services.mtproto_sync --login")
            else:
                if not st.telegram_phone:
                    await client.connect()
                    if not await client.is_user_authorized():
                        await client.disconnect()
                        raise RuntimeError(
                            "Сессия не авторизована. Варианты: интерактивный "
                            "логин `python -m app.services.mtproto_sync --login` "
                            "(одноразово, создаёт .session или печатает "
                            "SESSION_STRING) либо TELEGRAM_PHONE/PHONE в .env")
                password = st.telegram_password or None
                await client.start(
                    phone=st.telegram_phone or None,
                    password=(lambda: password) if password else None)
            me = await client.get_me()
            self._client = client
            self._me = me
            # НЕ логируем телефон/ключи — только публичный id/username
            logger.info("MTProto: подключено (user id={} @{})",
                        me.id, me.username or "-")
            return client

    @property
    def me(self) -> Any | None:
        return self._me

    def is_connected(self) -> bool:
        c = self._client
        return bool(c is not None and getattr(c, "is_user_authorized", lambda: False)())

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.debug("MTProto disconnect: {}", exc)
            self._client = None
            self._me = None


holder = MtprotoClientHolder()


async def resolve_channel_entity(target: str | int) -> Any:
    """Entity канала/группы по username или внутреннему id (-100...).

    Для числовых id без access_hash GetFullChannel требует точное разрешение;
    пробуем username/@ссылку, затем ищем среди диалогов аккаунта.
    """
    from telethon.tl.types import Channel
    client = await holder.get()
    s = str(target).strip()
    if s.startswith("-100"):
        inner = int(s[4:])
    elif s.lstrip("-").isdigit():
        inner = int(s)
    else:
        return await client.get_entity(s if s.startswith("@") else f"@{s}")
    # id → entity через диалоги (участник видит канал в списке чатов)
    async for d in client.iter_dialogs():
        if isinstance(d.entity, Channel) and d.entity.id == inner:
            return d.entity
    raise RuntimeError(
        f"Канал {target} не найден среди диалогов MTProto-аккаунта — "
        "добавьте аккаунт в канал или используйте CHANNEL_USERNAME")
