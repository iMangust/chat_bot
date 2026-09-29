from __future__ import annotations

import asyncio
import time

from loguru import logger

from app.config import get_settings

_MIN_SEND_INTERVAL = 3.0
_last_sent: float = 0.0

def _throttled() -> bool:
    global _last_sent
    now = time.monotonic()
    if now - _last_sent < _MIN_SEND_INTERVAL:
        return True
    _last_sent = now
    return False

async def start_userbot(bot) -> asyncio.Task | None:
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
    except Exception as exc:
        logger.warning("UserBot не запущен: {}: {} (нужен --login или "
                       "MTPROTO_SESSION_STRING)", type(exc).__name__, str(exc)[:180])
        return None

    from telethon import events
    from telethon.tl.types import Message as TLMessage

    @client.on(events.NewMessage(incoming=True, func=lambda e: not e.is_private))
    async def _on_incoming(event: events.NewMessage.Event) -> None:
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
        except Exception as exc:
            logger.debug("UserBot reply skipped: {}", type(exc).__name__)

    logger.info("🤖 UserBot активен (режим {}) — отвечаю от аккаунта id={}",
                mode, holder.me.id if holder.me else "?")
    return asyncio.current_task()

async def stop_userbot() -> None:
    from app.services.mtproto_client import holder
    await holder.disconnect()
    logger.info("UserBot остановлен")
