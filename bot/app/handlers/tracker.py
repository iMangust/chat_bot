"""Хендлеры трекера активности (сообщения и реакции в группах).

Важно: aiogram 3 по умолчанию НЕ получает reaction-события — нужно включить
allowed_updates=["message","message_reaction","chat_member"] при поллинге
(см. main.py) и иметь права админа/бот-менеджера в группе для реакций.

Также здесь — служебный хендлер «нет времени на красоту»: уведомления о
разблокированных достижениях отправляются отдельным сообщением (важное событие),
остальное — редактированием одного сообщения.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.types import Message, ReactionCount
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.config import get_settings
from app.services.activity import ActivityService

router = Router(name="activity")


def _is_tracked(chat_id: int) -> bool:
    ids = get_settings().tracked_chat_ids
    return not ids or chat_id in ids


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def track_group_message(message: Message, session: AsyncSession) -> None:
    """Пишет каждое сообщение группы в лог; засчитывает по антифрод-правилам."""
    if message.from_user is None or message.from_user.is_bot:
        return
    if not _is_tracked(message.chat.id):
        return
    # сервисные сообщения (вступление, выход, закреп) не считаем
    if (message.new_chat_members or message.left_chat_member or message.pinned_message
            or message.new_chat_title or message.new_chat_photo or message.delete_chat_photo):
        return

    text = message.text or message.caption
    media_type = None
    for attr in ("photo", "video", "voice", "audio", "document", "sticker", "animation"):
        if getattr(message, attr, None):
            media_type = attr
            break

    svc = ActivityService(session)
    entry = await svc.process_group_message(
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=text,
        has_media=media_type is not None,
        media_type=media_type,
        is_reply=message.reply_to_message is not None,
        mentions_count=len(message.entities or []) and sum(
            1 for e in (message.entities or []) if e.type == "mention"
        ) or 0,
        is_command=bool(text and text.startswith("/")),
    )
    if entry and entry.is_counted:
        # секретка «Сова»: 3–5 ночи. Время берём С ИСТОЧНИКА СОБЫТИЯ —
        # message.date это UTC-время, когда сообщение реально отправлено в чате
        # (а не время обработки апдейта ботом/сервером). Смещение — из конфига.
        msg_local = message.date.replace(tzinfo=timezone.utc) + timedelta(
            hours=get_settings().tz_offset_hours)
        if 3 <= msg_local.hour <= 5:
            from app.services.achievements import AchievementService
            ach = AchievementService(session)
            unlocked = await ach.unlock_by_code(message.from_user.id, "night_owl")
            if unlocked:
                try:
                    await message.bot.send_message(
                        message.from_user.id,
                        f"🎭 Секретное достижение: {unlocked.icon} <b>{unlocked.title}</b>!\n"
                        f"{unlocked.description}",
                    )
                except Exception as e:  # ЛС закрыты — не критично
                    logger.debug("no DM for secret achievement: {}", e)


@router.message(F.reactions)
async def track_reactions(message: Message, session: AsyncSession) -> None:
    """message_reaction: засчитываем каждую новую реакцию на сообщение автора."""
    if message.chat.type not in ("group", "supergroup") or not _is_tracked(message.chat.id):
        return
    r = message.reactions
    if r is None or r.total_count == 0:
        return
    # автор сообщения, на которое поставили реакцию, нам неизвестен без fetch'а —
    # берём из кеша/БД при наличии; иначе считаем только «поставил» (from_user).
    # MVP: определяем to_user через getChatMessage (один вызов на событие).
    to_user = None
    try:
        src = await message.bot.get_message(message.chat.id, r.message_id)
        if src.from_user and not src.from_user.is_bot:
            to_user = src.from_user.id
    except Exception:
        pass

    emoji = ""
    new_reaction: ReactionCount | None = None
    for rc in r.new_reaction:
        if rc.total_count > 0:
            new_reaction = rc
            emoji = rc.emoji or "👍"
            break
    if new_reaction is None or to_user is None:
        return

    svc = ActivityService(session)
    await svc.process_reaction(
        from_user=message.from_user.id, to_user=to_user,
        chat_id=message.chat.id, message_id=r.message_id, emoji=emoji,
    )
