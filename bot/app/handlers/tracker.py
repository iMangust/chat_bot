"""Хендлеры трекера активности (сообщения, медиа и реакции в группах).

Важно: aiogram 3 по умолчанию НЕ получает reaction-события — нужно включить
allowed_updates=[… "message_reaction", "message_reaction_count"] при поллинге
(см. main.py) и иметь права админа с правом Manage Reaction Messages в группе.

Реакции приходят апдейтом ``message_reaction`` (объект MessageReactionUpdated),
а НЕ сообщением с полем ``reactions`` — поэтому хендлер зарегистрирован через
``router.message_reaction``. Старый фильтр ``F.reactions`` никогда не срабатывал,
и бот молча игнорировал все реакции. Анонимные реакции каналов приходят отдельным
апдейтом ``message_reaction_count`` (только счётчики без авторов) — см. второй
хендлер ниже.

Типы сообщений разделяются честно: текст / фото / видео / видеокружок
(video_note) / голосовое (voice) / аудио / стикер / анимация(GIF) / документ —
каждому типу своя метка в media_type и свой XP-бонус.
"""
from __future__ import annotations

import contextlib
from datetime import timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.types import (Message, MessageReactionUpdated,
                           MessageReactionCountUpdated, ReactionTypeEmoji)
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.config import get_settings
from app.services.activity import ActivityService

router = Router(name="activity")

# Порядок важен: video_note проверяется ДО video (кружок — отдельный тип),
# animation (GIF) до document (в Telegram GIF технически является document).
MEDIA_ATTRS: tuple[str, ...] = (
    "photo", "video_note", "video", "voice", "audio",
    "sticker", "animation", "document", "contact", "location", "poll",
)

# XP-бонус за «социальные» форматы: чем больше усилий, тем дороже
MEDIA_XP_BONUS: dict[str, int] = {
    "voice": 2,       # голосовые — самый живой формат
    "video_note": 2,  # видеокружок
    "video": 2,
    "photo": 1,
    "animation": 1,   # гиф
    "sticker": 1,
    "audio": 1,
    "document": 0,
    "contact": 0,
    "location": 0,
    "poll": 1,
}


def _is_tracked(chat_id: int) -> bool:
    ids = get_settings().tracked_chat_ids
    return not ids or chat_id in ids


def detect_media_type(message: Message) -> str | None:
    """Возвращает тип медиа сообщения (photo/video_note/voice/…), либо None."""
    for attr in MEDIA_ATTRS:
        if getattr(message, attr, None):
            return attr
    return None


def _author(message: Message) -> int | None:
    """Автор засчитываемого сообщения.

    В каналах пост публикует сам канал (from_user=None), а фактический автор —
    sender_chat. Берём и его, иначе канальная активность «испаряется».
    """
    if message.from_user is not None and not message.from_user.is_bot:
        return message.from_user.id
    if message.sender_chat is not None and not getattr(message.sender_chat, "is_bot", False):
        return message.sender_chat.id
    return None


@router.message(F.chat.type.in_({"group", "supergroup", "channel"}))
async def track_group_message(message: Message, session: AsyncSession) -> None:
    """Пишет каждое сообщение группы/канала в лог; засчитывает по антифрод-правилам.

    ВАЖНО: каналы (где бот админ) тоже трэкаются — иначе «активность в
    канале» не начислялась бы вовсе.
    """
    author = _author(message)
    if author is None:
        return
    if not _is_tracked(message.chat.id):
        return
    # сервисные сообщения (вступление, выход, закреп) не считаем
    if (message.new_chat_members or message.left_chat_member or message.pinned_message
            or message.new_chat_title or message.new_chat_photo or message.delete_chat_photo):
        return

    text = message.text or message.caption
    media_type = detect_media_type(message)

    # автор написал сообщение в отслеживаемом чате впервые — вероятно, это
    # новый подписчик канала (если chat_member-событие боту недоступно).
    # Заносим в очередь приветствий (идемпотентно).
    if message.from_user is not None and not message.from_user.is_bot:
        from app.handlers.welcome import add_pending_subscriber
        await add_pending_subscriber(
            session, author, message.chat.id,
            first_name=message.from_user.first_name or "",
            username=message.from_user.username)

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
    if entry and entry.is_counted:
        # секретка «Сова»: 3–5 ночи. Время берём С ИСТОЧНИКА СОБЫТИЯ —
        # message.date это UTC-время, когда сообщение реально отправлено в чате
        # (а не время обработки апдейта ботом/сервером). Смещение — из конфига.
        msg_local = message.date.replace(tzinfo=timezone.utc) + timedelta(
            hours=get_settings().tz_offset_hours)
        if 3 <= msg_local.hour <= 5:
            # ВАЖНО: автор может быть sender_chat (пост от имени канала),
            # тогда message.from_user is None — раньше это роняло хендлер
            # с AttributeError прямо на засчитанном сообщении.
            # v1.5.9: без мгновенного DM — разблокировка уходит в общую очередь
            # уведомлений (доставится планировщиком). Награда уже начислена в
            # AchievementService.unlock_by_code -> _grant_rewards.
            from app.services.achievements import AchievementService
            ach = AchievementService(session)
            unlocked = await ach.unlock_by_code(author, "night_owl")
            if unlocked:
                logger.info("🎭 secret night_owl unlocked for {}", author)


def _reaction_emoji(rt) -> str | None:
    """Достаёт emoji из ReactionTypeEmoji; для кастомных (пользовательских)
    реакций возвращает заглушку, чтобы они тоже учитывались."""
    if isinstance(rt, ReactionTypeEmoji):
        return rt.emoji
    if getattr(rt, "type", "") == "custom_emoji":
        return "💎"  # кастомная реакция (Premium) — засчитываем как ❤‑подобную
    return None


async def _fetch_author_via_forward(bot: Bot, chat_id: int, message_id: int) -> int | None:
    """Автор сообщения через forward_message в ЛС самого бота.

    В aiogram 3.x нет прямого «get message by id» (ограничение Bot API).
    Пересланное в личные сообщения бота сообщение содержит исходного автора
    в ``from_user``; сразу удаляем пересылку, чтобы не копить мусор. Если бот
    не может читать чат/пересылать (нет доступа, канал с анонимными постами) —
    возвращаем None, начисление по такой реакции пропускаем.
    """
    me = await bot.get_me()
    try:
        fwd = await bot.forward_message(chat_id=me.id, from_chat_id=chat_id, message_id=message_id)
    except Exception as exc:  # Forbidden / MessageCan'tBeForwarded / удалено
        logger.debug("reaction fetch failed (forward): {}", exc)
        return None
    author = fwd.sender_chat or None
    uid: int | None = fwd.from_user.id if (fwd.from_user and not fwd.from_user.is_bot) else None
    if uid is None and author is not None and author.type in ("private", "channel"):
        # пересылка от имени канала/группы — персонального автора нет, не накручиваем
        uid = None
    with contextlib.suppress(Exception):
        await bot.delete_message(chat_id=me.id, message_id=fwd.message_id)
    return uid


@router.message_reaction()
async def track_reaction_update(update: MessageReactionUpdated,
                                session: AsyncSession) -> None:
    """Апдейт message_reaction: засчитываем НОВУЮ реакцию автора на сообщение.

    Приходит MessageReactionUpdated (chat, message_id, old_reaction,
    new_reaction, user|actor_chat). Считаем только добавление: если список
    стал длиннее/изменился в плюс — это дарение реакции получателю.
    """
    # реакции бывают и в каналах — трэкаем те же чаты, что и сообщения
    if update.chat.type not in ("group", "supergroup", "channel") or not _is_tracked(update.chat.id):
        return
    # юзер может быть None у анонимных админов — тогда берём actor_chat или выходим
    from_user_id: int | None = None
    if getattr(update.user, "id", None) is not None:
        from_user_id = update.user.id
    elif update.actor_chat is not None:
        # реакция от имени канала: автор действия — сам канал, не считаем
        return
    if from_user_id is None:
        return

    old_set = {_reaction_emoji(r) for r in (update.old_reaction or [])} - {None}
    new_set = {_reaction_emoji(r) for r in (update.new_reaction or [])} - {None}
    added = new_set - old_set
    if not added:
        return  # снял реакцию или ничего не изменилось — не начисляем
    emoji = sorted(added)[0]

    # автор сообщения, на которое поставили реакцию.
    # Источник №1 — локальный лог (быстрый, без сетевого запроса).
    # Источник №2 — Telegram API: в aiogram 3.x у Bot нет метода get_message
    # (Bot API 7.0 не отдаёт «прочитать сообщение» напрямую), поэтому делаем
    # forward_message в ЛС самого бота и берём автора из пересланного
    # сообщения. Прежний вызов bot.get_message падал с AttributeError и
    # молча терял все реакции на сообщения, которых бот не видел в логе.
    to_user: int | None = None
    svc = ActivityService(session, bot=update.bot)
    try:
        to_user = await svc.activity.get_message_author(update.chat.id, update.message_id)
    except Exception as exc:  # noqa: BLE001 — БД-фолбэки не должны ронять учёт
        logger.debug("reaction author lookup failed (db): {}", exc)
    if to_user is None:
        to_user = await _fetch_author_via_forward(update.bot, update.chat.id, update.message_id)
    if to_user is None or to_user == from_user_id:
        return  # неясный автор или само-реакция — не накручиваем

    await svc.process_reaction(
        from_user=from_user_id, to_user=to_user,
        chat_id=update.chat.id, message_id=update.message_id, emoji=emoji,
    )


@router.message_reaction_count()
async def track_reaction_count_update(update: MessageReactionCountUpdated) -> None:
    """Апдейт message_reaction_count: АНОНИМНЫЕ счётчики реакций в каналах.

    Telegram присылает ``message_reaction_count`` вместо ``message_reaction``,
    когда реакции анонимные (включённая опция «Анонимные реакции» в канале) —
    в апдейте есть только chat, message_id и суммарные счётчики
    (``ReactionCount``: тип + total_count), но НЕТ автора действия и НЕТ
    автора сообщения. Поэтому построчный учёт «кто кому поставил» здесь
    невозможен: пересчёт дельт по счётчикам дал бы ложные начисления
    (неизвестно, чья это реакция), а ``get_message`` в анонимном канале не
    вернёт автора. Ограничение зафиксировано в Bot API 7.0 осознанно —
    корректный способ учитывать персональные реакции описан выше
    (``message_reaction`` + автор из ``ChatMessageLog``). Логируем событие,
    чтобы админ видел факт анонимных реакций и мог сверить статистику.
    """
    if not _is_tracked(update.chat.id):
        return
    total = sum(rc.total_count for rc in (update.reaction_count or []))
    logger.info(
        "анонимные реакции: chat={} msg={} всего={}",
        update.chat.id, update.message_id, total,
    )
