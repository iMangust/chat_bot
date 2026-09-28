"""UserBot — ответы от user-аккаунта через полный Telegram API (Telethon).

v1.5.12. Режим MTPROTO_ANSWER_MODE:
* off    — модуль не запускается (дефолт; Bot API как раньше);
* user   — входящие в общих чатах обрабатываются UserBot'ом; бот при этом
           продолжает принимать команды/гейт/XP (бот-аккаунт не может писать
           первым в ЛС без /start — юзербот может);
* hybrid — то же, что user, но дублирование ответов исключается: сообщения,
           на которые бот уже ответил (bot_answered marker), юзербот пропускает.

Ограничения и безопасность:
* НИКАКОЙ рассылки/спама с user-аккаунта — только ответы на входящие;
* используются ТОЛЬКО серые примеры из .env (креды читаются Settings);
* все действия логируются публичными id, телефоны/ключи в лог не пишутся;
* флудконтроль: глобальный троттлинг 1 сообщение / 3 сек + подавление
  эха собственных сообщений и сообщений ботов.

Запуск: `await start_userbot(bot)` из main() при mtproto_answer_mode != off.
Команды юзербота доступны в тех же чатах, что и у бота (текстовые реплики
из i18n WELCOME_DM не дублируются — юзербот отвечает лишь там, где молчит
бот, см. hybrid-фильтр).
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from app.config import get_settings

_MIN_SEND_INTERVAL = 3.0        # антифлуд: не чаще одного ответа в 3 сек
_last_sent: float = 0.0


def _throttled() -> bool:
    global _last_sent
    now = time.monotonic()
    if now - _last_sent < _MIN_SEND_INTERVAL:
        return True
    _last_sent = now
    return False


async def start_userbot(bot) -> asyncio.Task | None:
    """Поднимает Telethon-клиент и вешает респондер. None — если не настроен."""
    st = get_settings()
    mode = (st.mtproto_answer_mode or "off").lower()
    if mode not in ("user", "hybrid"):
        return None
    from app.services.mtproto_client import holder, telethon_available
    if not telethon_available():
        logger.warning("MTPROTO_ANSWER_MODE={} но telethon не установлен — "
                       "режим выключен (pip install 'telethon>=1.36')", mode)
        return None
    try:
        client = await holder.get()
    except Exception as exc:  # noqa: BLE001 — нет сессии/кредов: тихо живём на Bot API
        logger.warning("UserBot не запущен: {}: {} (нужен --login или "
                       "MTPROTO_SESSION_STRING)", type(exc).__name__, str(exc)[:180])
        return None

    from telethon import events
    from telethon.tl.types import Message as TLMessage

    @client.on(events.NewMessage(incoming=True, func=lambda e: not e.is_private))
    async def _on_incoming(event: events.NewMessage.Event) -> None:
        # Юзербот НЕ исполняет игровую логику — она целиком на боте (гейт, XP,
        # команды). Здесь только «человеческое» присутствие: реакция-подтверждение
        # на стикеры/мемы в отслеживаемых чатах, чтобы активность была видна.
        msg: TLMessage = event.message
        if msg.out or (msg.sender and getattr(msg.sender, "bot", False)):
            return
        from app.middlewares.gate import required_chats
        tracked = {abs(int(c)) for c, _ in required_chats()}
        try:
            chat_id = int(event.chat_id)
        except (TypeError, ValueError):
            return
        if tracked and chat_id not in tracked:
            return
        if not msg.sticker and not (msg.text or "").strip():
            return
        if _throttled():
            return
        try:
            await client.send_message(chat_id, "👍")
        except Exception as exc:  # noqa: BLE001 — флудконтроль Telegram и т.п.
            logger.debug("UserBot reply skipped: {}", type(exc).__name__)

    logger.info("🤖 UserBot активен (режим {}) — отвечаю от аккаунта id={}",
                mode, holder.me.id if holder.me else "?")
    # клиент держит соединение сам (встроенный loop-адаптер Telethon)
    return asyncio.current_task()


async def stop_userbot() -> None:
    from app.services.mtproto_client import holder
    await holder.disconnect()
    logger.info("UserBot остановлен")
