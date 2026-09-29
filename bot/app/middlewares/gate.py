"""Глобальный доступ (v2.0): только ЛС и только подписчики отслеживаемых чатов.

Правила бота:
1. Взаимодействие с ботом — исключительно в личных сообщениях. В группах и
   каналах бот молчит: не отвечает на команды и кнопки, ничего не пишет
   (пассивный трекер активности остаётся — см. handlers/tracker.py).
2. Подписка хотя бы на ОДИН из обязательных чатов (TRACKED_CHAT_IDS; при
   пустом списке — CHANNEL_USERNAME / CHANNEL_CHAT_ID) = полный доступ.
   Нет подписки — просьба подписаться. Никаких приветствий/рассылок: бот
   первым не пишет никогда (старая welcome-механика удалена в v2.0).

Как проверяется подписка (по возрастанию стоимости):
  1) положительный кэш (SUBSCRIBE_CACHE_SEC);
  2) Bot API getChatMember — статус member/administrator/creator сразу даёт
     доступ и фиксируется в реестре (быстрый путь, без обращения к БД);
  3) MTProto-глаза (userbot): при включённой приватности («скрытый список
     участников») Bot API отдаёт состоящих в чате людей как left/kicked/
     restricted — Telethon видит их честно. Проба идёт ПОСЛЕ реестра:
     дешёвый SELECT раньше дорогого RPC;
  4) реестр channel_subscribers: туда записи попадают только из достоверных
     источников членства (события chat_member/new_chat_members, сообщение
     автора в чате, MTProto-сканы) — «нет прав» у реального участника
     исключён даже при недоступном MTProto;
  5) MTProto-проба конкретного пользователя + живой скан всех участников
     (разовый, с дебаунсом): если ни события, ни синк человека ещё не
     видели, гейт сам обновляет реестр и пропускает его — доступ больше не
     зависит от того, успел ли пройтись cron-дельта-синк.
Любой сбой Telegram => fail-open: бот не имеет права «мирать» в ЛС из-за
недоступности API. Если ни один чат не настроен — доступ разрешён (dev-режим),
админ получает разовое предупреждение.

v2.0.1 (лог «участники по-прежнему не могут взаимодействовать»): Bot API
getChatMember при включённой приватности канала отдаёт РЕАЛЬНЫХ участников со
статусом 'left' — старая версия гейта после такого ответа почти мгновенно
выдавала заглушку «подпишись», потому что обе страховки были сломаны:
  * реестр был пуст (события chat_member приходят только на вступление, а
     дельта-синк заносит лишь новых);
  * MTProto-проба вызывалась внутри цикла и падала молча (см. ниже).
Теперь порядок проверок гарантированно спасает подписчика: сначала реестр
(дешёвый SELECT), затем MTProto-проба, затем живой скан участников.

Отладочный лог каждого решения гейта (уровень DEBUG) — чтобы вопрос
«почему N не может пользоваться ботом» отвечался одной строкой в логе.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

_bg_tasks: set = set()

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import CallbackQuery, Message, TelegramObject, User
from loguru import logger

from app.config import get_settings

SUBSCRIBE_CACHE_SEC = 300
_NEG_TTL_SEC = 15
_GRANTED_KEY = "sub_granted"

_pos_cache: dict[Any, float] = {}
_neg_cache: dict[Any, float] = {}
_warned_no_admin: set[str] = set()
_warned_no_gating: set[str] = set()

gate_last_reason: dict[str, Any] = {}

class _SyntheticPrivateChat:
    """Заглушка чата: приватный тип для ЛС-колбэков без message.chat."""
    type = ChatType.PRIVATE

def required_chats() -> list[tuple[str, str]]:
    """Чаты, подписка хотя бы на ОДИН из которых обязательна: [(id, username)].

    Источник — TRACKED_CHAT_IDS (см. config): взаимодействие разрешено только
    подписчикам одного из отслеживаемых канала/группы. Если список пуст,
    используем CHANNEL_CHAT_ID / CHANNEL_USERNAME (одиночный канал).

    v2.0.5 (важное): канал и его группа обсуждения объединяются в СПИСОК
    всегда. Раньше при заполненном TRACKED_CHAT_IDS канал проверялся лишь
    тогда, когда был перечислен там явно; если админ указывал только группу,
    гейт требовал членства исключительно в группе — и человек, состоящий в
    канале (но не в группе), получал ложный отказ. Тот же класс ошибки, что
    «фильтр по chat.type» в v1.6.x: проверочная цепочка обязана включать ВСЕ
    известные боту чаты проекта.
    """
    st = get_settings()
    seen: set[str] = set()
    chats: list[tuple[str, str]] = []

    def _add(value: object, uname: str = "") -> None:
        key = str(value)
        if value and key not in seen:
            seen.add(key)
            chats.append((key, uname))

    _add(str(st.channel_chat_id) if st.channel_chat_id else "",
         st.channel_username or "")
    for cid in st.tracked_chat_ids:
        _add(str(cid))
    if not chats and (st.channel_chat_id or st.channel_username):
        chats.append((str(st.channel_chat_id or ""), st.channel_username or ""))
    return chats

def subscribe_kb() -> "Any":
    """Клавиатура для неподписанных: ссылка на канал + проверка подписки."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    st = get_settings()
    rows = []
    ch = st.channel_username or ""
    if not ch:
        for cid, uname in required_chats():
            if uname:
                ch = uname
                break
            if cid.startswith("-100"):
                ch = f"+{cid[4:]}"
                break
    if ch:
        rows.append([InlineKeyboardButton(text=f"📢 Подписаться: t.me/{ch}",
                                          url=f"https://t.me/{ch}")])
    rows.append([InlineKeyboardButton(text="✅ Я подписался — проверить",
                                      callback_data="gate:check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def reset_subscribe_cache(user_id: int | None = None) -> None:
    """Сбрасывает кэш проверки подписки (по пользователю или весь целиком)."""
    if user_id is None:
        _pos_cache.clear()
        _neg_cache.clear()
    else:
        _pos_cache.pop(user_id, None)
        _neg_cache.pop(user_id, None)

async def _notify_admin(bot, text_key: str, uid: str, text: str) -> None:
    """Предупреждает админа (ADMIN_IDS) о проблеме конфигурации гейта.

    Разовость обеспечивает вызывающий (_notify_no_* добавляют ключ в множества
    ДО планирования таска): иначе первый же тап помечал бы канал «предупреждён»,
    а реальная отправка ещё даже не началась — и при сбое Telegram/подавлении
    исключения админ не узнал бы никогда (так и было до v2.0.1).
    """
    ids = get_settings().admin_ids
    if not ids:
        return
    try:
        await bot.send_message(ids[0], text)
    except Exception as exc:
        logger.warning("gate admin notice for {} failed to send: {}", uid, exc)

async def known_subscriber_in_db(user_id: int) -> bool:
    """Числится ли пользователь в реестре с ПОДТВЕРЖДЁННЫМ членством.

    Возвращает False при любой ошибке БД — гейт тогда опирается на остальные
    источники (MTProto / живой скан / fail-open).
    """
    return bool(await known_subscriber_ids([user_id]))

async def known_subscriber_ids(user_ids: list[int] | set[int]) -> set[int]:
    """Множество id из user_ids, у которых в channel_subscribers есть НЕПУСТОЙ
    список чатов ``chats`` (достоверный сигнал присутствия).

    v2.0.3 (важное уточнение безопасности): раньше достаточно было ЛЮБОЙ
    строки — но строку создаёт и /start без подтверждения подписки
    (ever_contacted, chats=[]), и старые welcome-записи после миграции.
    Такая «строка без чата» открывала бы доступ неподписанному человеку,
    стоит ему один раз нажать «Проверить подписку». Право доступа доказывает
    именно непустой ``chats`` (запись туда делают только события вступления,
    сообщения автора в чате, MTProto-сканы и живые проверки API/MTProto).

    Один запрос вместо N (batch). Ошибка БД => пустое множество
    (поведение как раньше — консервативно).
    """
    ids = {int(u) for u in user_ids if u}
    if not ids:
        return set()
    try:
        from app.db.models import ChannelSubscriber
        from app.db.session import session_factory
        from sqlalchemy import select

        async with session_factory() as session:
            stmt = select(ChannelSubscriber.user_id,
                          ChannelSubscriber.chats).where(
                ChannelSubscriber.user_id.in_(ids))
            rows = (await session.execute(stmt)).all()
        return {int(uid) for uid, chats in rows if list(chats or [])}
    except Exception as exc:
        logger.debug("gate db-fallback failed for {}: {}", user_ids, exc)
        return set()

def _remember_membership(bot, user_id: int, cid: str, source: str) -> None:
    """Фиксирует подтверждённое членство в реестре (фоновая задача).

    Реестр — страховочный источник доступа (следующие тапы проходят по нему,
    даже когда Bot API «ослепает» на приватности). Идемпотентно; ошибки БД
    не влияют на выдачу доступа.

    v2.0.6: для критического пути (Bot API ответил 'member' во время
    проверки доступа) запись идёт НАПРЯМУЮ в БД (add_membership_sql), а не
    через ORM-UPSERT: параллельный писатель (MTProto-скан/событие чата) мог
    перезаписать строку поверх stale-снимка и стереть только что доказанное
    членство — человек оставался без страховки до следующего подтверждения.
    """
    async def _run() -> None:
        try:
            if source == "bot-api" and cid:
                from app.db.repositories import SubscriberRepository
                from app.db.session import session_factory
                async with session_factory() as session:
                    await SubscriberRepository(session).add_membership_sql(
                        user_id, int(cid))
            else:
                from app.handlers.access import register_member
                await register_member(user_id, int(cid) if cid else None,
                                      real_event=True)
        except Exception as exc:
            logger.warning("gate: record membership {} ({}) failed: {}",
                           user_id, source, exc)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        _bg_tasks.add(t := loop.create_task(_run()))
        t.add_done_callback(_bg_tasks.discard)

async def _live_scan(bot, user_id: int) -> bool:
    """Живой скан участников + повторная проверка реестра.

    Тонкость v2.0.1: ensure_registry_fresh возвращает «скан запускался», а не
    «человек найден» (например, чат уже сканировали минуту назад — дебаунс).
    Поэтому реестр перепроверяется ВСЕГДА после попытки скана: иначе свежий
    подписчик получал бы заглушку до следующего cron-дельта-синка.

    v2.0.2 (лог 23:41): раньше при сбое ensure_registry_fresh (дебаунс,
    недоступный MTProto, ошибка БД) результат молча становился False — и
    живой человек из реестра получал «🔒 подпишись». Теперь перед выдачей
    отказа реестр перечитывается НАПРЯМУЮ (SELECT без кэша): запись,
    созданную синком секундой ранее, гейт обязан увидеть сразу.
    """
    try:
        from app.handlers.access import ensure_registry_fresh
        await ensure_registry_fresh(bot, user_id)
    except Exception as exc:
        logger.warning("gate: live scan fallback failed for {}: {}", user_id, exc)
    return await known_subscriber_in_db(user_id)

def is_subscribed_cached(user_id: int) -> bool | None:
    """Вердикт гейта из кэша без RPC: True/False или None (нет свежей записи)."""
    now = time.monotonic()
    pos = _pos_cache.get(user_id)
    if pos is not None and pos > now:
        return True
    neg = _neg_cache.get(user_id)
    if neg is not None and neg > now:
        return False
    return None

async def _mtproto_status(target: str | int, user_id: int):
    """MTProto-проба статуса пользователя. Возвращает ('ok', status) либо
    ('error', причина).

    v2.0.1: get_chat_member_status сам по себе «никогда не бросает» и
    возвращает None при любой проблеме, но импорт/вызов могут упасть и сами
    по себе (Telethon не установлен, битый конфиг, таймаут сети). Раньше эта
    ошибка терялась в DEBUG-логе MTProto-клиента, а гейт молча блокировал
    реального подписчика («участники не могут взаимодействовать»). Теперь
    сбой различим: ('error',...) логируется явно и переводит проверку на
    следующий источник (реестр → живой скан), а не в заглушку.

    v2.0.4 (подробный дебаг): kind='silent' — проба вернула None без
    исключения (клиент не в чате / PARTICIPANTS_TOO_LARGE / entity не
    найдена). Причина берётся из last_scan_error и попадает в лог отказа
    вместо абстрактного «probe returned None».
    """
    try:
        from app.services.mtproto_client import get_chat_member_status
        st = await get_chat_member_status(target, user_id)
        if st is None:
            reason = ""
            with contextlib.suppress(Exception):
                from app.services.mtproto_client import last_scan_error
                reason = await last_scan_error()
            return ("silent", reason or "MTProto вернул None (нет клиента/прав/"
                                        "entity; точная причина в логе DEBUG)")
        return ("ok", st)
    except Exception as exc:
        return ("error", f"{type(exc).__name__}: {str(exc)[:120]}")

async def _registry_row_state(user_id: int) -> str:
    """Диагностика строки реестра для логов гейта: существует ли, что в chats."""
    try:
        from app.db.models import ChannelSubscriber
        from app.db.session import session_factory
        async with session_factory() as s:
            row = await s.get(ChannelSubscriber, int(user_id))
        if row is None:
            return "NO ROW"
        return f"row exists, chats={list(row.chats or [])!r}"
    except Exception as exc:
        return f"read failed: {type(exc).__name__}: {str(exc)[:80]}"

async def is_channel_subscribed(bot, user_id: int) -> bool:
    """True — пользователь подписан хотя бы на ОДИН обязательный чат
    (TRACKED_CHAT_IDS; при пустом списке — CHANNEL_USERNAME/CHANNEL_CHAT_ID),
    либо проверка недоступна (fail-open).

    Положительный результат кэшируется на SUBSCRIBE_CACHE_SEC, отрицательный —
    на _NEG_TTL_SEC (короткий, чтобы «Я подписался» срабатывало почти сразу).
    Любая ошибка API => fail-open: бот обязан оставаться отзывчивым даже при
    недоступном канале/сбое Telegram — молчание в ЛС недопустимо.

    Ключевое правило v2.0.1: ОТРИЦАТЕЛЬНЫЙ ответ Bot API ('left'/'kicked'/
    'restricted') НЕ является доказательством отсутствия подписки. При
    включённой приватности канала так отдаются РЕАЛЬНЫЕ участники. Поэтому
    перед блокировкой запускается цепочка страховок — реестр (дешёвый SELECT)
    → MTProto-проба пользователя → живой скан участников. Заглушка «подпишись»
    выдаётся только когда все источники промолчали.
    """
    chats = required_chats()
    if not chats:
        logger.error("subscription gate disabled: TRACKED_CHAT_IDS/CHANNEL_* are empty — "
                     "anyone can use the bot")
        _notify_no_gating(bot)
        return True
    now = time.monotonic()
    pos = _pos_cache.get(user_id)
    if pos is not None and pos > now:
        logger.debug("gate: allow {} (positive cache)", user_id)
        return True
    neg = _neg_cache.get(user_id)
    if neg is not None and neg > now:
        logger.debug("gate: deny {} (negative cache, TTL {}с)", user_id,
                     int(_NEG_TTL_SEC))
        return False
    logger.info("gate: checking subscription for {} in chats {}",
                user_id, [c[0] or c[1] for c in chats])
    api_error = False
    api_status: dict[str, str] = {}
    negative_api = False
    for cid, uname in chats:
        target = f"@{uname}" if uname else cid
        uid_key = uname or cid
        try:
            member = await bot.get_chat_member(target, user_id)
        except TelegramForbiddenError as exc:
            api_error = True
            api_status[target] = f"FORBIDDEN ({str(exc)[:60]})"
            if uid_key not in _warned_no_admin:
                logger.warning("cannot check subscription for {}: {} — fail-open",
                               target, exc)
            _notify_no_admin(bot, uid_key)
            continue
        except TelegramAPIError as exc:
            api_error = True
            api_status[target] = f"API ERROR {type(exc).__name__}: {str(exc)[:60]}"
            logger.warning("subscription check failed for {} ({}): skip chat",
                           target, exc)
            continue
        api_status[target] = getattr(member, "status", "?")
        if member.status in ("member", "administrator", "creator"):
            _pos_cache[user_id] = time.monotonic() + SUBSCRIBE_CACHE_SEC
            _neg_cache.pop(user_id, None)
            _remember_membership(bot, user_id, cid, "bot-api")
            logger.info("gate: allow {} (Bot API '{}' in {})",
                        user_id, member.status, target)
            return True
        negative_api = True
    if api_error and not negative_api:
        logger.info("gate: allow {} (fail-open: API unavailable — {})",
                    user_id, api_status)
        return True
    if not negative_api:
        if await known_subscriber_in_db(user_id):
            logger.info("gate: allow {} — present in channel_subscribers registry "
                        "(Bot API returned non-membership status: {})",
                        user_id, api_status)
            _pos_cache[user_id] = time.monotonic() + SUBSCRIBE_CACHE_SEC
            return True
        if api_error:
            logger.info("gate: {} — API mixed ({}); running fallback chain",
                        user_id, api_status)
        else:
            if await _mtproto_and_scan_fallback(bot, user_id, chats, api_status):
                return True
            _neg_cache[user_id] = time.monotonic() + _NEG_TTL_SEC
            logger.warning(
                "gate: DENY {} — all sources silent | Bot API: {} | registry: {} "
                "| MTProto/live-scan: см. строки выше",
                user_id, api_status, await _registry_row_state(user_id))
            return False

    if await _mtproto_and_scan_fallback(bot, user_id, chats, api_status):
        return True
    _neg_cache[user_id] = time.monotonic() + _NEG_TTL_SEC
    logger.warning(
        "gate: DENY {} — all sources silent | Bot API: {} | registry: {} "
        "| MTProto/live-scan: см. строки выше",
        user_id, api_status, await _registry_row_state(user_id))
    return False

async def _mtproto_and_scan_fallback(bot, user_id: int,
                                     chats: list[tuple[str, str]],
                                     api_status: dict[str, str] | None = None,
                                     ) -> bool:
    """Страховочная цепочка для случаев, когда Bot API НЕ подтвердил членство
    (приватность канала, статус 'bot', сбой по одному из чатов).

    Порядок: реестр (дешёвый SELECT) → MTProto-проба пользователя → живой
    скан участников. True — доступ доказан и закэширован; False — все
    источники промолчали (лог отказа печатает вызывающий).

    v2.0.4 (подробный дебаг): каждый шаг пишет INFO/WARNING со своей причиной,
    а перед финальным отказом сверяет полноту последнего живого скана с
    точным числом участников чата (GetFullChannel). Если скан собрал заметно
    меньше половины — это тихая недопустимость выборки Telethon (см.
    mtproto_client._LAST_SCAN_ERROR), и бот честно говорит об этом вместо
    ложного «не подписан».
    """
    if await known_subscriber_in_db(user_id):
        logger.info("gate: allow {} — not visible via Bot API (privacy?) "
                    "but present in channel_subscribers registry", user_id)
        _pos_cache[user_id] = time.monotonic() + SUBSCRIBE_CACHE_SEC
        return True
    mt_notes: list[str] = []
    for cid, uname in chats:
        mt_target = uname or cid
        s_mt = str(mt_target).lstrip("@")
        if not (s_mt.startswith("-100") or s_mt.lstrip("-").isdigit()):
            continue
        kind, st = await _mtproto_status(mt_target, user_id)
        if kind == "error":
            mt_notes.append(f"{mt_target}: ИСКЛЮЧЕНИЕ {st}")
            continue
        if kind == "silent":
            mt_notes.append(f"{mt_target}: молчит ({st})")
            continue
        if st in ("member", "administrator", "creator"):
            logger.info("gate: allow {} — visible in {} only via MTProto "
                        "(Bot API said left-ish — privacy?)",
                        user_id, mt_target)
            _pos_cache[user_id] = time.monotonic() + SUBSCRIBE_CACHE_SEC
            _neg_cache.pop(user_id, None)
            _remember_membership(bot, user_id, cid, "mtproto")
            return True
        mt_notes.append(f"{mt_target}: MTProto='{st}'")
    if mt_notes:
        logger.warning("gate: MTProto probe did not confirm {} ({}); "
                       "trying live participants scan",
                       user_id, "; ".join(mt_notes))
    if await _live_scan(bot, user_id):
        logger.info("gate: allow {} — found by live participants scan", user_id)
        _pos_cache[user_id] = time.monotonic() + SUBSCRIBE_CACHE_SEC
        return True
    with contextlib.suppress(Exception):
        from app.handlers.access import last_scan_stats
        from app.services.mtproto_client import chat_participants_count
        seen, total = last_scan_stats()
        if seen is not None:
            expect = None
            for cid, uname in chats:
                expect = await chat_participants_count(uname or cid)
                if expect:
                    break
            if expect and seen < max(2, expect // 2):
                logger.error(
                    "gate: ⚠️ живой скан собрал только {} из {} участник(ов) — "
                    "выборка Telethon неполная (PARTICIPANTS_TOO_LARGE/entity/"
                    "пагинация). Отказ может быть ЛОЖНЫМ: включите показ списка "
                    "участников в группе или проверьте, что MTProto-аккаунт "
                    "состоит в чатах", seen, expect)
    try:
        from app.handlers.access import last_scan_stats as _lss
        seen, total = _lss()
    except Exception:
        seen = total = None
    gate_last_reason.clear()
    gate_last_reason.update({
        "user_id": user_id,
        "api_status": dict(api_status or {}),
        "registry": await _registry_row_state(user_id),
        "mtproto": "; ".join(mt_notes) or "нет MTProto-проб (чат не числовой?)",
        "scan_error": await last_scan_error_safe(),
        "scan_seen": seen, "scan_total_in_chat": total,
    })
    return False

async def last_scan_error_safe() -> str:
    """Причина молчания MTProto для диагностики (никогда не бросает)."""
    with contextlib.suppress(Exception):
        from app.services.mtproto_client import last_scan_error
        return await last_scan_error() or ""
    return ""

def _notify_no_admin(bot, uid: str) -> None:
    """Разово предупреждает админа, что бот не может проверять чат."""
    if uid in _warned_no_admin:
        return
    _warned_no_admin.add(uid)
    try:
        t = asyncio.ensure_future(_notify_admin(
            bot, "no_admin", uid,
            f"⚠️ Не могу проверять доступ ({uid}): бот должен быть "
            "администратором канала с правом «Добавлять администраторов» "
            "(Add Admins). Пока доступ работает в режиме разрешения (fail-open)."))
    except RuntimeError:
        _warned_no_admin.discard(uid)
        return
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)

def _notify_no_gating(bot) -> None:
    key = "no-gating"
    if key in _warned_no_gating:
        return
    _warned_no_gating.add(key)
    try:
        t = asyncio.ensure_future(_notify_admin(
            bot, "no_gating", key,
            "⚠️ Проверка подписки отключена: не заданы TRACKED_CHAT_IDS и "
            "CHANNEL_USERNAME/CHANNEL_CHAT_ID. Любой пользователь может "
            "взаимодействовать с ботом — настройте обязательные чаты."))
    except RuntimeError:
        _warned_no_gating.discard(key)
        return
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)

_ENTRY_COMMANDS = {"start", "help"}

def _is_entry_command(message: Message, bot_username: str = "") -> bool:
    """Команда входа (/start, /help) — работает и для неподписанных.

    v2.0.6: принимает и адресную форму «/start@Sasha_Ovs_bot» (Telegram
    подставляет @username бота в кнопки «Начать» из deep-link/меню команд;
    раньше такая команда НЕ считалась входом и глушилась гейтом даже у
    подписчика — ровно боевой лог 10:45: «gate: allow …», а ответа нет).
    Команда, адресованная ДРУГОМУ боту (/start@OtherBot), входом не считается.
    """
    text = message.text or ""
    if not text.startswith("/"):
        return False
    body = text[1:].split()[0]
    cmd, _, named = body.partition("@")
    cmd = cmd.lower()
    if cmd not in _ENTRY_COMMANDS:
        return False
    if named and bot_username:
        return named.lower().lstrip("@") == bot_username.lower().lstrip("@")
    return True

async def _resolve_bot_username(bot) -> str:
    """Юзернейм бота (для распознавания адресных команд «/start@NameBot»).

    v2.0.6: после `await bot.init` aiogram уже знает имя (bot.username /
    bot.me) — берём его без сетевого вызова; иначе один раз дёргаем getMe и
    кэшируем по экземпляру. Пустая строка = «имя неизвестно» → любая
    адресная форма считается обращённой к нам (fail-open: подписчик не
    потеряет вход из-за недоступности getMe).
    """
    name = getattr(bot, "username", None) or getattr(
        getattr(bot, "me", None), "username", None)
    if name:
        return name.lower().lstrip("@")
    key = id(bot)
    cached = _BOT_USERNAME_CACHE.get(key)
    if cached is not None:
        return cached
    resolved = ""
    try:
        me = await bot.get_me()
        if getattr(me, "username", None):
            resolved = me.username.lower().lstrip("@")
    except Exception as exc:
        logger.debug("gate: get_me failed (username unknown): {}", exc)
    _BOT_USERNAME_CACHE[key] = resolved
    return resolved

_BOT_USERNAME_CACHE: dict[int, str] = {}

def _addressed_to_this_bot(text: str, bot_username: str) -> bool:
    """/start@OtherBot — команда ДРУГОМУ боту из общего чата.

    v2.0.6 (боевой лог 10:45): Telegram доставляет такие апдейты нашему боту
    (он состоит в группе), а гейт считал их «командами» и глушил молча —
    человек видел только безмолвие. Для служебных сообщений это безразлично,
    но ЛС-текст вида «/start@Sasha_Ovs_bot» — обращение именно к нам, и оно
    обязано дойти до хендлеров.
    """
    body = text[1:].split()[0] if text.startswith("/") else ""
    if "@" not in body:
        return True
    name = body.split("@", 1)[1].lower()
    return not bot_username or name == bot_username.lower().lstrip("@")

def _is_serviceable_group_message(event) -> bool:
    """Групповое сообщение, которое ведут служебные (безмолвные) хендлеры.

    Пропускаем к диспетчеру только служебные события: приход/уход участника
    (учёт подписчиков для гейта) и обычные текстовые/медиа-сообщения
    (пассивный трекер активности ничего не пишет в чат). Команды (/start
    и т.п.) в группах глотаются целиком — бот на них молчит.
    """
    if not isinstance(event, Message):
        return False
    if event.new_chat_members or event.left_chat_member:
        return True
    if event.text and event.text.startswith("/"):
        return False
    return not (event.pinned_message or event.new_chat_title
                or event.new_chat_photo or event.delete_chat_photo)

def _target_user(event) -> User | None:
    if isinstance(event, CallbackQuery):
        return event.from_user
    if isinstance(event, Message):
        return event.from_user
    return None

class AccessGateMiddleware(BaseMiddleware):
    """Outer-middleware на все апдейты: приватные чаты + подписка на канал."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)

        chat = getattr(event, "chat", None) or getattr(
            getattr(event, "message", None), "chat", None)
        if chat is None and isinstance(event, CallbackQuery):
            chat = (getattr(event.from_user, "_private_chat", None)
                    or _SyntheticPrivateChat())
        if chat is None:
            return await handler(event, data)

        if chat.type != ChatType.PRIVATE:
            if not isinstance(event, Message):
                with contextlib.suppress(Exception):
                    await event.answer()
                return None
            if _is_serviceable_group_message(event):
                return await handler(event, data)
            return None

        user = _target_user(event)
        if user is None:
            return await handler(event, data)
        if user.is_bot:
            return None

        try:
            subscribed = await is_channel_subscribed(data["bot"], user.id)
        except Exception as exc:
            logger.warning("subscription gate crashed for {}: {} — allow", user.id, exc)
            subscribed = True

        text = getattr(event, "text", None) or ""
        bot_name = await _resolve_bot_username(data["bot"]) \
            if text.startswith("/") else ""
        if isinstance(event, Message) and text.startswith("/") \
                and not _addressed_to_this_bot(text, bot_name):
            return await handler(event, data)

        exempt = isinstance(event, Message) and _is_entry_command(event, bot_name)
        if not exempt and not subscribed:
            ch = get_settings().channel_username
            link = f"t.me/{ch}" if ch else ""
            text_out = ["🔒 Взаимодействие с ботом недоступно:",
                        "ты не подписан ни на наш канал, ни на группу обсуждения."]
            if link:
                text_out.append(f"\n📢 Подпишись ({link}) — и возвращайся, я жду!")
            else:
                text_out.append("\n📢 Подпишись на канал/группу — и возвращайся, я жду!")
            text_out.append("После подписки нажми «Проверить» или отправь /start.")
            text = "\n".join(text_out)
            if isinstance(event, CallbackQuery):
                await event.answer("Сначала подпишись на канал 📢", show_alert=True)
                await event.message.edit_text(text, reply_markup=subscribe_kb())
            else:
                await event.answer(text, reply_markup=subscribe_kb())
            return None

        data[_GRANTED_KEY] = True
        return await handler(event, data)
