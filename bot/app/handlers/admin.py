"""Управление «полным Telegram API» (MTProto) из чата с ботом.

v1.5.12. Только для ADMIN_IDS, только в ЛС (гейт и фильтр чата).

Команды:
    /mtproto   — статус Telethon-настройки (без показа секретов!)
    /syncnow   — немедленная полная синхронизация участников канала
                 (первый запуск; остальные — дельтой) + flush приветствий
"""
from __future__ import annotations

import asyncio

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

from app.config import get_settings

router = Router(name="mtproto_admin")


def _is_admin(message: Message) -> bool:
    ids = get_settings().admin_ids
    return bool(ids) and message.from_user is not None and message.from_user.id in ids


@router.message(F.chat.type == "private", Command("mtproto"))
async def cmd_mtproto_status(message: Message) -> None:
    if not _is_admin(message):
        return
    st = get_settings()
    from app.services import mtproto_client as mc
    lines = ["🔌 <b>Telegram API (MTProto / Telethon)</b>", ""]
    lines.append(f"• telethon установлен: {'✅' if mc.telethon_available() else '❌ (pip install telethon>=1.36)'}")
    has_id = bool(st.telegram_api_id)
    has_hash = bool(st.telegram_api_hash)
    lines.append(f"• API_ID: {'✅ задан' if has_id else '❌ нет (API_ID / TELEGRAM_API_ID в .env)'}")
    lines.append(f"• API_HASH: {'✅ задан' if has_hash else '❌ нет (API_HASH / TELEGRAM_API_HASH в .env)'}")
    sess = "строковая сессия (MTPROTO_SESSION_STRING)" if st.mtproto_session_string \
        else f"файл {st.mtproto_session}.session" if mc.session_configured() else "❌ нет"
    lines.append(f"• Сессия: {sess}")
    lines.append(f"• Подключено сейчас: {'✅' if mc.holder.is_connected() else '— (ленивое подключение)'}")
    lines.append(f"• Режим ответов: {st.mtproto_answer_mode}")
    lines.append(f"• Автосинк при старте: {'вкл' if st.mtproto_autosync else 'выкл'}, "
                 f"дельта каждые {st.mtproto_sync_minutes} мин" if st.mtproto_sync_minutes
                 else "• Дельта-синк по расписанию: выкл")
    lines.append("")
    lines.append("Настройка ключей: https://my.telegram.org → API development tools. "
                 "Используйте ВТОРОЙ аккаунт. Логины: "
                 "<code>python -m app.services.mtproto_sync --login</code>")
    await message.answer("\n".join(lines))


@router.message(F.chat.type == "private", Command("syncnow"))
async def cmd_syncnow(message: Message, bot: Bot) -> None:
    if not _is_admin(message):
        return
    st = get_settings()
    # «/syncnow full» — принудительно полная синхронизация; иначе autosync
    # решает сам (пустая база ⇒ полная, есть данные ⇒ дельта)
    text = (message.text or "").lower()
    first_run = "full" in text or (st.mtproto_autosync and "delta" not in text)
    msg = await message.answer(
        f"⏳ Запускаю MTProto-синхронизацию участников ({'полная' if first_run else 'дельта'})…")
    from app.services.mtproto_sync import sync_subscribers
    try:
        res = await asyncio.wait_for(sync_subscribers(first_run=first_run), timeout=300)
    except Exception as exc:  # noqa: BLE001
        logger.error("/syncnow failed: {}", exc)
        await msg.edit_text(f"❌ Синхронизация не удалась: <code>{exc}</code>")
        return
    # сразу раздаём приветствия из обновлённой очереди
    welcomed = 0
    try:
        from app.db.session import session_factory
        from app.handlers.welcome import welcome_pending_subscribers
        async with session_factory() as session:
            welcomed = await welcome_pending_subscribers(bot, session, limit=20)
    except Exception as exc:  # noqa: BLE001
        logger.warning("/syncnow welcome flush failed: {}", exc)
    await msg.edit_text(
        "✅ Синхронизация завершена:\n"
        f"• участников найдено: {res['total']}\n"
        f"• новых в базе: {res['added']}\n"
        f"• старых пропущено: {res['skipped_old']}\n"
        f"• приветствий отправлено сейчас: {welcomed}")
