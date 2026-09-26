"""Хендлер приветствия новичков в группе (new_chat_members).

Сценарий:
1. Бот видит новых участников в отслеживаемой группе.
2. Пытается отправить каждому в ЛС персональное приветствие с кнопкой «Начать».
3. Если ЛС закрыты (TelegramForbiddenError) — новичок увидит подсказку
   только у себя (в группу бот не пишет ничего).

Реферал: награда пригласившему начисляется ОДНО место — в ActivityService
при первой засчитанной активности новичка (см. services/activity.py,
_credit_referral). Здесь мы только подхватываем deep-link-маркер из системного
сообщения «User joined Telegram by invite link» и сохраняем связку, если её
не удалось разобрать в /start бота.
"""
from __future__ import annotations

import contextlib
import html

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter,
)
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.config import get_settings
from app.db.repositories import UserRepository
from app.services.achievements import AchievementService
from app.handlers.start import WELCOME_DM
from app.keyboards.inline import welcome_start_button

router = Router(name="welcome")


def _is_tracked(chat_id: int) -> bool:
    ids = get_settings().tracked_chat_ids
    return not ids or chat_id in ids


def parse_invite_marker(text: str | None) -> int | None:
    """Достаёт tg_id пригласившего из текста «...invite_<id>...» (или None)."""
    if not text or "invite_" not in text:
        return None
    try:
        return int(text.split("invite_", 1)[1].split()[0])
    except (ValueError, IndexError):
        return None


@router.message(F.new_chat_members)
async def on_new_members(message: Message, bot: Bot, session: AsyncSession) -> None:
    """Приветствие новичков группы — лично каждому в ЛС (в чат — ни слова).

    Параллельно каждый новичок заносится в channel_subscribers как pending:
    если он уже подписан на канал (частый случай — пришёл из канала),
    скан/следующее событие доставит ему и канальное приветствие.
    """
    if message.chat.type not in ("group", "supergroup") or not _is_tracked(message.chat.id):
        return

    users = UserRepository(session)
    greeters: list[str] = []
    inviter = parse_invite_marker(message.text)

    for member in message.new_chat_members:
        if member.is_bot:
            continue
        # регистрируем «заготовку» — onboarded=False до нажатия «Начать»
        await users.get_or_create(member.id, member.first_name, member.username)
        # фолбэк-сигнал подписки: человек в отслеживаемом чате — вероятно,
        # он и в канале; welcome_pending_subscribers сам сверит с API
        await add_pending_subscriber(
            session, member.id, message.chat.id,
            first_name=member.first_name or "", username=member.username)
        # реферал: если новичок пришёл по deep-link `start=invite_<tg_id>` —
        # сохраняем связку; сама награда выдаётся после первой активности
        if inviter and inviter != member.id:
            if await users.set_referrer(member.id, inviter):
                logger.info("referral link stored: inviter={} new={}", inviter, member.id)
        try:
            await bot.send_message(
                member.id,
                WELCOME_DM.format(name=html.escape(member.first_name or "друг"),
                                  channel_line=_channel_line_safe()),
                reply_markup=welcome_start_button(),
            )
            greeters.append(f"<a href='tg://user?id={member.id}'>{html.escape(member.first_name or 'друг')}</a>")
            logger.info("welcome DM sent to {}", member.id)
        except TelegramForbiddenError:
            # ЛС закрыты — попросим написать сами (упоминание соберём ниже)
            greeters.append(html.escape(f"@{member.username or member.first_name}")
                         + " (ЛС закрыты — напиши мне /start!)")
        except TelegramRetryAfter as e:
            logger.warning("rate limited while welcoming {}: sleep {}", member.id, e.retry_after)
        except TelegramAPIError as e:
            logger.error("failed to welcome {}: {}", member.id, e)

    # В группы/каналы бот ничего не пишет (правило v1.5.3): приветствие —
    # только в ЛС; тем, кто закрыл ЛС, видна подсказка написать /start сами.
    # Канальное приветствие доставляем сразу, не дожидаясь фонового скана:
    # welcome_pending_subscribers сам сверит членство с API и учтёт welcomed_at.
    with contextlib.suppress(Exception):
        await welcome_pending_subscribers(bot, session, limit=5)


def _channel_line_safe() -> str:
    ch = get_settings().channel_username
    return f"📢 Наш канал: t.me/{ch}\n" if ch else ""


# ---------------------------------------------------------------------------
# Приветствие НОВЫХ ПОДПИСЧИКОВ КАНАЛА
# ---------------------------------------------------------------------------
# Ограничение Bot API: Telegram НЕ присылает событие «человек подписался на
# канал». Реальные источники сигнала:
#   1) chat_member-апдейт канала — если бот там админ с правом «Manage users»
#      и "chat_member" в allowed_updates (самый точный путь);
#   2) первое сообщение пользователя в привязанной группе/канале;
#   3) фоновый скан get_chat_member_count (см. tasks/scheduler.scan_channel_members).
# Все три ведут в add_pending_subscriber + welcome_pending_subscribers, а
# welcomed_at в channel_subscribers гарантирует ровно одно приветствие.

CHANNEL_WELCOME_DM_DEFAULT = (
    "👋 Привет, {name}! Ты подписался на наш канал — добро пожаловать!\n\n"
    "{channel_line}"
    "Я местный бот-компаньон 🐾: тут начисляют XP и монеты за активность,\n"
    "выдают достижения, есть питомец-тамагочи, топы и еженедельная арена.\n\n"
    "Жми «Начать» — покажу главное меню, это займёт минуту."
)


def channel_welcome_text() -> str:
    """Текст приветствия подписчика канала (из .env или дефолт)."""
    st = get_settings()
    tpl = st.channel_welcome_text or CHANNEL_WELCOME_DM_DEFAULT
    channel_line = (f"📢 Канал: t.me/{st.channel_username}\n\n"
                    if st.channel_username else "")
    try:
        return tpl.format(name="{name}", channel_line=channel_line,
                          channel=st.channel_username or "")
    except (KeyError, IndexError):  # свой шаблон с неизвестными плейсхолдерами
        return tpl


async def add_pending_subscriber(session: AsyncSession, user_id: int,
                                 chat_id: int, first_name: str = "",
                                 username: str | None = None,
                                 reset_welcome: bool = False) -> bool:
    """Заносит подписчика в базу; True — если он ждёт приветствия.

    reset_welcome=True уместен только для РЕАЛЬНЫХ событий вступления
    (chat_member/new_chat_members): человек присоединился к новому чату —
    разрешаем ещё одно приветствие. Для сигналов «косвенного присутствия»
    (/start, трекер сообщений) сброс запрещён: иначе каждое действие
    возвращает уже приветствованного пользователя в pending-очередь и он
    получает повторное DM-приветствие (двойные приветствия v1.5.6).
    """
    from app.db.repositories import SubscriberRepository
    subs = SubscriberRepository(session)
    added = await subs.add_if_new(
        user_id, chat_id, first_name=first_name, username=username,
        reset_welcome=reset_welcome)
    if added:
        await session.commit()
        logger.info("new channel subscriber registered: {} ({})", user_id, username or first_name)
    return added


async def welcome_pending_subscribers(bot: Bot, session: AsyncSession,
                                      limit: int = 5) -> int:
    """Шлёт приветствия в ЛС неободрённым подписчикам. Возвращает число отправленных.

    Перед отправкой статус проверяется через Telegram (getChatMember):
    pending-записи появляются из разных сигналов (первое сообщение в группе,
    вступление, скан), и только подтверждённое членство в канале является
    основанием для приветствия. Отправленное приветствие помечается
    welcomed_at — дублей не будет. TelegramForbiddenError (ЛС закрыты)
    оставляет запись pending: скан доприветствует, когда ЛС откроются.
    """
    from app.db.repositories import SubscriberRepository, UserRepository
    # gate.is_channel_subscribed берёт обязательные чаты из config при каждом
    # вызове — monkeypatch на функцию тестов работает как задумано
    from app.middlewares.gate import is_channel_subscribed, required_chats
    st = get_settings()
    if not st.welcome_channel_enabled:
        return 0
    if not required_chats():
        # ни один канал/чат не настроен — «подписчиков» верифицировать негде,
        # слать приветствие всем подряд нельзя
        logger.debug("channel welcome skipped: no tracked chats configured")
        return 0
    subs = SubscriberRepository(session)
    users = UserRepository(session)
    sent = 0
    for sub in await subs.pending_welcomes(limit=max(limit * 4, 20)):
        if sent >= limit:
            break
        # приветствуем только реально подписанных на канал
        if not await is_channel_subscribed(bot, sub.user_id):
            continue
        name = html.escape(sub.first_name or sub.username or "друг")
        text = channel_welcome_text().replace("{name}", name)
        try:
            await bot.send_message(sub.user_id, text,
                                   reply_markup=welcome_start_button())
        except TelegramForbiddenError:
            logger.debug("subscriber {} closed DMs — will retry on scan", sub.user_id)
            continue
        except TelegramRetryAfter as e:
            logger.warning("rate limited welcoming subscriber {}; pause {}", sub.user_id, e.retry_after)
            break
        except TelegramAPIError as e:
            logger.error("failed to welcome subscriber {}: {}", sub.user_id, e)
            continue
        # регистрируем «заготовку» пользователя, чтобы кнопка «Начать»
        # и /start подхватили уже знакомую систему анкету
        await users.get_or_create(sub.user_id, sub.first_name or "друг", sub.username)
        await subs.mark_welcomed(sub.user_id)
        await session.commit()
        sent += 1
        logger.info("channel welcome DM sent to {}", sub.user_id)
    return sent


@router.chat_member(F.new_chat_member.status.in_(["member", "administrator"]))
async def on_channel_join(update: ChatMemberUpdated, bot: Bot,
                          session: AsyncSession) -> None:
    """Новичок пришёл в канал/группу, где бот — админ (chat_member-апдейт).

    Заносим в channel_subscribers и сразу пробуем поприветствовать в ЛС.
    Боты и сам бот игнорируются; condition по old status не ставим — при
    перезапуске/пропуске апдейтов старый статус бывает «устаревшим», и жёсткий
    фильтр молча съедал приветствия. Идемпотентность гарантирует welcomed_at
    (ровно одно приветствие), а welcome_pending_subscribers дополнительно
    сверяет членство в канале через API.
    """
    member = update.new_chat_member
    if member.user.is_bot or member.user.id == bot.id:
        return
    ids = get_settings().tracked_chat_ids
    # Бот — админ и канала, и группы: chat_member приходит из обоих. Приветствуем
    # события отслеживаемых чатов; при пустом списке tracked — любой канал.
    if not (update.chat.type == "channel" or not ids or update.chat.id in ids):
        return
    # Реальное событие вступления: если человек уже приветствовался ради
    # ДРУГОГО отслеживаемого чата — разрешаем ещё одно приветствие (один DM
    # на чат). Сброс делает add_if_new(reset_welcome=True), поэтому отдельный
    # reset_welcome здесь не нужен и повторных сбросов не допускает.
    await add_pending_subscriber(
        session, member.user.id, update.chat.id,
        first_name=member.user.first_name or "", username=member.user.username,
        reset_welcome=True)
    # Доставка: welcome_pending_subscribers сам сверит участие хотя бы в одном
    # обязательном чате через API (см. gate.required_chats) и учтёт welcomed_at.
    await welcome_pending_subscribers(bot, session, limit=5)

