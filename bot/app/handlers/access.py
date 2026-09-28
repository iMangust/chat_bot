"""Доступ к боту (v2.0): подписан — значит доступ есть. Никаких приветствий.

Старая welcome-механика (фоновые очереди, DM-рассылки, backoff для закрытых
ЛС) удалена целиком: бот НИЧЕГО не пишет первым и не «ожидает» доставки.
Правило одно: человек состоит хотя бы в одном обязательном чате
(TRACKED_CHAT_IDS; при пустом списке — CHANNEL_CHAT_ID/CHANNEL_USERNAME) —
и взаимодействует с ботом в ЛС свободно.

Знание о членстве собирается из достоверных источников:
  * события Telegram (bot_added_to_chat / chat_member / new_chat_members);
  * первое сообщение автора в отслеживаемом чате (пассивный трекер);
  * MTProto-синхронизация участников (видит «анонимных», которых Bot API
    отдаёт left/restricted из-за приватности).
Все сигналы идут в реестр channel_subscribers (одна строка на человека,
список чатов ``chats``) — см. SubscriberRepository.record_membership().

Проверка при тапе (gate.is_channel_subscribed), по скорости:
  1) положительный кэш;
  2) Bot API getChatMember (быстро, без БД);
  3) MTProto-глаза (приватность);
  4) реестр channel_subscribers;
  5) живой полный скан участников (разовый дебаунс на чат) — лечит случай
     «бот админ, но ни одного события вступления не поймал»;
  6) сбой API => fail-open: бот обязан оставаться отзывчивым.

Хендлеры этого модуля работают ДО AccessGateMiddleware (регистрируются как
outer-middleware на апдейты), поэтому служебные события групп доходят до них
даже когда диспетчерские хендлеры отрезаны гейтом.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message, TelegramObject
from loguru import logger

from app.config import get_settings
from app.db.session import session_factory
from app.middlewares.gate import required_chats

_SCAN_COOLDOWN_SEC = 600          # не чаще одного живого скана на чат за 10 мин
_scan_busy: set[str] = set()      # чаты со сканом «в полёте»
_scan_last: dict[str, float] = {} # monotonic-момент последнего скана на чат

# v2.0.4 (подробный дебаг): статистика последнего живого скана — сколько
# участников реально собрано и во сколько их оценивает сам Telegram
# (GetFullChannel.participants_count). Разброс «собрано < половины» означает
# неполную выборку Telethon (PARTICIPANTS_TOO_LARGE / битый entity / обрыв
# пагинации) — гейт использует это для честного WARNING перед отказом.
_last_scan_seen: int | None = None
_last_scan_total: int | None = None


def last_scan_stats() -> tuple[int | None, int | None]:
    """(собрано участнков, точное число в чате) последнего живого скана."""
    return _last_scan_seen, _last_scan_total


def watched_chat_ids() -> set[int]:
    """Все чаты, присутствие в которых даёт доступ (пусто = не фильтруем)."""
    st = get_settings()
    ids: set[int] = set(st.tracked_chat_ids or ())
    if st.channel_chat_id:
        ids.add(int(st.channel_chat_id))
    return ids


def _is_watched(chat_id: int | None) -> bool:
    if chat_id is None:
        return False
    ids = watched_chat_ids()
    return not ids or int(chat_id) in ids


async def register_member(user_id: int, chat_id: int | None = None, *,
                          first_name: str = "", username: str | None = None,
                          real_event: bool = True,
                          contacted: bool = False) -> None:
    """Единая точка входа всех сигналов членства/контакта в реестре.

    Идемпотентна и NEVER бросает наружу: сбой БД не должен ломать обработку
    апдейта или проверку доступа (человек останется виден по API-проверке).

    v2.0.4: запись выполняется СИНХРОННО (await до конца вызова). Раньше это
    был fire-and-forget таск — между событием вступления и первым обращением
    к боту строка могла ещё не существовать, и гейт отказывал человеку,
    который только что подписался (гонка «событие → /start» в боёвке).
    """
    try:
        from app.db.repositories import SubscriberRepository
        async with session_factory() as session:
            await SubscriberRepository(session).record_membership(
                user_id, chat_id, first_name=first_name, username=username,
                real_event=real_event, contacted=contacted)
        # v2.0.4 (дебаг): видимость записи реестра сразу после события —
        # «кто именно и с какими чатами попал в базу» отвечался только
        # гаданием по логам синка (боёвка 23:41)
        if real_event and chat_id is not None:
            logger.info("access: registry {} <- chat {} (event)",
                        user_id, chat_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("access: register {}@{} failed: {}: {}",
                       user_id, chat_id, type(exc).__name__,
                       str(exc)[:200])


# ---------------------------------------------------------------------------
# Живой MTProto-скан: «подписчик, которого мы ещё не видели ни в одном
# событии» получает доступ сразу, а не после cron-дельты (до 60 минут).
# ---------------------------------------------------------------------------
def _numeric_target(cid_or_uname: str) -> int | None:
    s = str(cid_or_uname).lstrip("@")
    if s.startswith("-100"):
        return int(s[4:])
    try:
        return int(s)
    except ValueError:
        return None


async def scan_chat_participants(target: str) -> int:
    """Заносит ВСЕХ участников одного чата в реестр. Возвращает число записей.

    source='mtproto' — достоверный сигнал присутствия (Telethon видит и
    скрытых приватностью). Ошибки глотаются: недоступный MTProto не должен
    влиять ни на доступ, ни на фоновые задачи.

    v2.0.4 (подробный дебаг): после скана сравнивает собранное с точным
    числом участников (GetFullChannel) и пишет WARNING при неполной выборке
    — иначе «добавлено 3 из 5» выглядело успехом, а пропущенные люди молча
    оставались без доступа (боёвка 23:41: синк рапортовал успех, гейт
    отказывал). Статистика хранится в last_scan_stats() для диагностики
    отказа гейта и команды /accessdebug.
    """
    global _last_scan_seen, _last_scan_total
    added = 0
    try:
        from app.services.mtproto_client import iter_all_participants
        from app.services.mtproto_sync import mtproto_configured
        if not mtproto_configured():
            return 0
        members = await iter_all_participants(target)
    except Exception as exc:  # noqa: BLE001
        logger.debug("access: MTProto scan {} failed: {}", target, exc)
        return 0
    cid = _numeric_target(target)
    for m in members or []:
        try:
            uid = int(m["id"])
        except (KeyError, TypeError, ValueError):
            continue
        await register_member(uid, cid, first_name=m.get("first_name") or "",
                              username=m.get("username"))
        added += 1
    _last_scan_seen = len(members or [])
    total: int | None = None
    with contextlib.suppress(Exception):
        from app.services.mtproto_client import chat_participants_count
        total = await chat_participants_count(target)
    _last_scan_total = total
    if added:
        logger.info("access: живой скан {} — {} участник(ов) в реестре",
                    target, added)
    if members and total and len(members) < max(2, total // 2):
        logger.error("access: ⚠️ скан {} собрал {} из ~{} участник(ов) — "
                     "выборка Telethon НЕПОЛНАЯ; часть подписчиков не получит "
                     "доступа из реестра (см. last_scan_error / настройки "
                     "приватности группы)", target, len(members), total)
    return added


async def ensure_registry_fresh(bot: Bot, user_id: int) -> bool:
    """Реестр молчит — пробуем один раз обновить его живым сканом.

    Вызывается из gate только когда Bot API НЕ подтвердил членство и записи
    тоже нет (то есть перед выдачей заглушки «подпишись»). Дебаунс на чат,
    чтобы не гонять Telethon на каждый тап.
    """
    from app.services.mtproto_sync import mtproto_configured
    if not mtproto_configured():
        return False
    now = time.monotonic()
    ran = False
    for cid, uname in required_chats():
        target = uname or cid
        key = str(target)
        if key in _scan_busy or _scan_last.get(key, 0.0) > now - _SCAN_COOLDOWN_SEC:
            continue
        _scan_busy.add(key)
        try:
            await scan_chat_participants(target)
            ran = True
        finally:
            _scan_busy.discard(key)
            _scan_last[key] = time.monotonic()
    return ran


# ---------------------------------------------------------------------------
# Служебные события членства (работают до гейта — см. AccessEventsMiddleware)
# ---------------------------------------------------------------------------
async def handle_new_chat_members(message: Message) -> None:
    """Новички группы обсуждения — достоверное событие присутствия.

    v2.0.5 (по логам боёвки): раньше принимались только типы group/supergroup.
    В каналах-рассылках Telegram тоже отдаёт new_chat_members (например, при
    добавлении человека в канал), и это событие терялось — реестр молчал до
    часового дельта-синка. Фильтр — строго по id отслеживаемых чатов
    (_is_watched), тип больше не учитываем: тот же класс регресса, что с
    «chat.type == channel» в v1.6.x.
    """
    if not _is_watched(message.chat.id):
        return
    for member in message.new_chat_members or []:
        if member.is_bot:
            continue
        await register_member(member.id, message.chat.id,
                              first_name=member.first_name or "",
                              username=member.username)


async def handle_chat_member(event: ChatMemberUpdated, bot_id: int | None) -> None:
    """chat_member: приход/уход участника канала И группы обсуждения.

    Фильтр строго по id: канал −100… и группа −100… имеют одинаковый тип
    "channel" (ветвление по type теряло события группы — регресс v1.6.x).
    Выход из чата запись не удаляем: утрата части членств не должна лишать
    доступа, если человек остался хотя бы в одном обязательном чате
    (актуальность доказывается API-проверкой гейта).
    """
    member = event.new_chat_member
    user = getattr(member, "user", None)
    if user is None or user.is_bot or (bot_id and user.id == bot_id):
        return
    status = getattr(member, "status", "")
    # v2.0.5 (по логам боёвки 23:12/23:40 «chat_member update ignored: …
    # member -> left»): Telegram отдаёт chat_member-события и для СЛУЖЕБНЫХ
    # переходов — например при включении анонимной публикации в канале все
    # участники получают administrator -> member, а при выключении — back.
    # Раньше тракталось так: «не member/admin/creator» → игнор. Из-за этого
    # реестр пополнялся только часовым MTProto-дельта-синком (в логах ровно
    # это и видно), а человек, обратившийся к боту в промежутке, получал
    # отказ. Любой апдейт членства в отслеживаемом чате — достоверный сигнал
    # присутствия автора В МОМЕНТ события; фиксируем его независимо от
    # перехода. Уход (left/kicked) тоже пишется как факт: право доступа всё
    # равно доказывает свежая проверка гейта (API/MTProto), а запись не даёт
    # ложного доступа тому, кто никогда здесь не был.
    if not _is_watched(event.chat.id):
        return
    await register_member(user.id, event.chat.id,
                          first_name=user.first_name or "",
                          username=user.username)
    logger.info("access: chat_member {} in {} ({}) — registered",
                user.id, event.chat.id, status)


class AccessEventsMiddleware(BaseMiddleware):
    """Outer-middleware: собирает знания о членстве ДО фильтрации гейтом.

    Глобальные outer-мидлвары выполняются в порядке регистрации, поэтому он
    ставится ПЕРЕД AccessGateMiddleware: иначе chat_member/new_chat_members
    отрезались бы гейтом и в реестр не попадали бы вовсе.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            if isinstance(event, ChatMemberUpdated):
                await handle_chat_member(event, data["bot"].id)
            elif isinstance(event, Message) and event.new_chat_members:
                await handle_new_chat_members(event)
        except Exception as exc:  # noqa: BLE001 — сбор сигналов не валит апдейт
            logger.warning("access events middleware failed: {}", exc)
        # v2.0.5 (по логам боёвки): каждое служебное сообщение о членстве в
        # отслеживаемом чате — достоверный сигнал присутствия автора. Трекер
        # активности пишет в реестр только ОБЫЧные сообщения и фильтрует
        # «сервисные» (вступление/выход/закреп), поэтому такие события здесь
        # раньше терялись целиком. Теперь фиксируем до гейта и независимо от
        # того, пропустит ли их диспетчер.
        if isinstance(event, Message) and getattr(event, "from_user", None) is not None:
            chat = event.chat
            if (chat is not None and chat.type != ChatType.PRIVATE
                    and _is_watched(chat.id)
                    and not event.from_user.is_bot
                    and (event.new_chat_members or event.left_chat_member)):
                with contextlib.suppress(Exception):
                    await register_member(event.from_user.id, chat.id,
                                          first_name=event.from_user.first_name or "",
                                          username=event.from_user.username)
        # v2.0.5 (подробный дебаг + страховка доступа): каждый тап в ЛС
        # фиксируется в реестре как «человек общается с ботом» — даже если
        # ни одно событие вступления/скан его не видели. Это же даёт /start
        # подписчика, у которого закрыты ЛС для бота (событий от него Bot API
        # не получает вовсе). Запись дешёвая (один SELECT+UPSERT раз в 5 мин
        # на пользователя), ошибки БД не влияют на выдачу доступа.
        user = event.from_user if isinstance(event, (Message, CallbackQuery)) else None
        chat = getattr(event, "chat", None) or getattr(
            getattr(event, "message", None), "chat", None)
        if (user is not None and not getattr(user, "is_bot", False)
                and getattr(user, "id", None)
                and (chat is None or getattr(chat, "type", None) == ChatType.PRIVATE)):
            await remember_contact(user.id, first_name=user.first_name or "",
                                   username=user.username)
        return await handler(event, data)


_CONTACT_THROTTLE_SEC = 300       # не чаще одной UPSERT-записи контакта на юзера
_contact_last: dict[int, float] = {}


async def remember_contact(user_id: int, *, first_name: str = "",
                           username: str | None = None) -> None:
    """Фиксирует в реестре факт общения человека с ботом (ЛС-тап).

    Право доступа ЭТО НЕ даёт (chats остаётся пустым — см. gate
    known_subscriber_ids), но строка с ever_contacted видна в диагностике
    /accessdebug и логах отказа: сразу различимы случаи «вообще неизвестен
    боту» vs «пишет боту, но членство нигде не подтверждено». Идемпотентно,
    с дебаунсом по времени; NEVER бросает наружу.
    """
    now = time.monotonic()
    if _contact_last.get(user_id, 0.0) > now - _CONTACT_THROTTLE_SEC:
        return
    _contact_last[user_id] = now
    try:
        from app.db.repositories import SubscriberRepository
        async with session_factory() as session:
            await SubscriberRepository(session).record_membership(
                user_id, None, first_name=first_name, username=username,
                real_event=False, contacted=True)
    except Exception as exc:  # noqa: BLE001 — диагностика не должна валить тап
        logger.debug("access: remember contact {} failed: {}", user_id, exc)
