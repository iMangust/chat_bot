from __future__ import annotations

import contextlib
from datetime import timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (Message, MessageReactionUpdated,
                           MessageReactionCountUpdated, ReactionTypeEmoji)
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.config import get_settings
from app.db.repositories import UserRepository
from app.services.activity import ActivityService

router = Router(name="activity")

MEDIA_ATTRS: tuple[str, ...] = (
    "photo", "video_note", "video", "voice", "audio",
    "sticker", "animation", "document", "contact", "location", "poll",
)

MEDIA_XP_BONUS: dict[str, int] = {
    "voice": 2,
    "video_note": 2,
    "video": 2,
    "photo": 1,
    "animation": 1,
    "sticker": 1,
    "audio": 1,
    "document": 0,
    "contact": 0,
    "location": 0,
    "poll": 1,
}

def _is_tracked(chat_id: int) -> bool:
    """Отслеживаемый чат? Конфиг TRACKED_CHAT_IDS — основной источник."""
    from app.handlers.access import is_watched
    if is_watched(chat_id):
        return True
    # Фолбэк: если в конфиге не перечислены трекаемые чаты (или чат ещё не
    # попал в реестр), считаем отслеживаемым любой чат, где бот состоит и
    # куда он получает сообщения. Иначе статистика молча не считается —
    # самый частый баг «написал сообщение в группу, но оно не засчиталось».
    try:
        from app.services import access as _acc
        ids = _acc.watched_chat_ids()
        if not ids:  # TRACKED_CHAT_IDS пуст — не блокируем учёт совсем
            return True
    except Exception:  # noqa: BLE001
        pass
    return False

def detect_media_type(message: Message) -> str | None:
    for attr in MEDIA_ATTRS:
        if getattr(message, attr, None):
            return attr
    return None

def _author(message: Message) -> int | None:
    if message.from_user is not None and not message.from_user.is_bot:
        return message.from_user.id
    if message.sender_chat is not None and not getattr(message.sender_chat, "is_bot", False):
        return message.sender_chat.id
    return None

@router.message(F.chat.type.in_({"group", "supergroup", "channel"}))
async def track_group_message(message: Message, session: AsyncSession) -> None:
    author = _author(message)
    if author is None:
        return
    if not _is_tracked(message.chat.id):
        return
    if (message.new_chat_members or message.left_chat_member or message.pinned_message
            or message.new_chat_title or message.new_chat_photo or message.delete_chat_photo):
        return

    text = message.text or message.caption
    media_type = detect_media_type(message)

    # Медиа без текста (голосовые, кружочки, стикеры, видео…) тоже должны
    # засчитываться: даём им условную длину, чтобы не отсеивались фильтром
    # min_message_length как «слишком короткие».
    if media_type and not (text or "").strip():
        text = "[media]"

    if message.from_user is not None and not message.from_user.is_bot:
        from app.services.access import remember_contact
        await remember_contact(
            author, first_name=message.from_user.first_name or "",
            username=message.from_user.username)

    # Диагностика: почему сообщение может НЕ засчитаться. Каждый молча
    # отброшенный кейс — это «написал в группу, а статистика не обновилась».
    user = await UserRepository(session).get(author)
    if user is None:
        logger.info("📊 трекер: пользователь {} ещё без реестровой записи — "
                    "будет создан автоматически (первое сообщение)", author)
    elif user.is_banned:
        logger.info("📊 трекер: пользователь {} забанен — не считаем", author)

    svc = ActivityService(session, bot=message.bot)
    entry = await svc.process_group_message(
        user_id=author,
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=text,
        has_media=media_type is not None,
        media_type=media_type,
        is_reply=message.reply_to_message is not None,
        mentions_count=sum(1 for e in (message.entities or []) if e.type == "mention"),
        is_command=bool(text and text.startswith("/")),
    )
    if entry is not None and not entry.is_counted:
        logger.info("📊 трекер: msg {} от {} в чате {} НЕ засчитана ({})",
                    message.message_id, author, message.chat.id,
                    entry.skip_reason)
    if entry and entry.is_counted:
        msg_local = message.date.replace(tzinfo=timezone.utc) + timedelta(
            hours=get_settings().tz_offset_hours)
        if 3 <= msg_local.hour <= 5:
            from app.services.achievements import AchievementService
            ach = AchievementService(session)
            unlocked = await ach.unlock_by_code(author, "night_owl")
            if unlocked:
                logger.info("🎭 secret night_owl unlocked for {}", author)

def _reaction_emoji(rt) -> str | None:
    if isinstance(rt, ReactionTypeEmoji):
        return rt.emoji
    if getattr(rt, "type", "") == "custom_emoji":
        return "💎"
    return None

async def _fetch_author_via_forward(bot: Bot, chat_id: int, message_id: int) -> int | None:
    me = await bot.get_me()
    try:
        fwd = await bot.forward_message(chat_id=me.id, from_chat_id=chat_id, message_id=message_id)
    except TelegramAPIError as exc:
        logger.debug("reaction fetch failed (forward): {}", exc)
        return None
    author = fwd.sender_chat or None
    uid: int | None = fwd.from_user.id if (fwd.from_user and not fwd.from_user.is_bot) else None
    if uid is None and author is not None and author.type in ("private", "channel"):
        uid = None
    with contextlib.suppress(Exception):
        await bot.delete_message(chat_id=me.id, message_id=fwd.message_id)
    return uid

@router.message_reaction()
async def track_reaction_update(update: MessageReactionUpdated,
                                session: AsyncSession) -> None:
    if update.chat.type not in ("group", "supergroup", "channel") or not _is_tracked(update.chat.id):
        return
    from_user_id: int | None = None
    if getattr(update.user, "id", None) is not None:
        from_user_id = update.user.id
    elif update.actor_chat is not None:
        return
    if from_user_id is None:
        return

    old_set = {_reaction_emoji(r) for r in (update.old_reaction or [])} - {None}
    new_set = {_reaction_emoji(r) for r in (update.new_reaction or [])} - {None}
    added = new_set - old_set
    if not added:
        return
    emoji = sorted(added)[0]

    to_user: int | None = None
    svc = ActivityService(session, bot=update.bot)
    try:
        to_user = await svc.activity.get_message_author(update.chat.id, update.message_id)
    except Exception as exc:
        logger.debug("reaction author lookup failed (db): {}", exc)
    if to_user is None:
        to_user = await _fetch_author_via_forward(update.bot, update.chat.id, update.message_id)
    if to_user is None or to_user == from_user_id:
        return

    await svc.process_reaction(
        from_user=from_user_id, to_user=to_user,
        chat_id=update.chat.id, message_id=update.message_id, emoji=emoji,
    )

@router.message_reaction_count()
async def track_reaction_count_update(update: MessageReactionCountUpdated) -> None:
    if not _is_tracked(update.chat.id):
        return
    total = sum(rc.total_count for rc in (update.reaction_count or []))
    logger.info(
        "анонимные реакции: chat={} msg={} всего={}",
        update.chat.id, update.message_id, total,
    )
