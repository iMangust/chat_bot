"""Реестр членства и проверка подписки: единственный источник истины для доступа.

Правило бота: доступ к взаимодействию в личных сообщениях имеет тот, кто
состоит хотя бы в одном обязательном чате проекта (канал и/или группа
обсуждения). Реестр channel_subscribers хранит по одной строке на человека со
списком чатов, где его присутствие подтверждено любым достоверным источником:

  * события Telegram chat_member / new_chat_members (бот — админ чата);
  * сообщения пользователя в отслеживаемом чате (пассивный трекер);
  * ответы Bot API getChatMember со статусом member/administrator/creator;
  * MTProto-пробы и полные сканы списка участников (Telethon видит людей,
    скрытых настройками приватности, которых Bot API отдаёт как left/kicked);
  * живые проверки кнопки «Я подписался — проверить» и диагностики /accessdebug.

Все записи идут через SubscriberRepository.record_membership — идемпотентный
UPSERT с нормализацией списка чатов. Ошибок наружу репозиторий не отдаёт:
сбой БД не должен ломать обработку апдейта или проверку доступа.
"""
from __future__ import annotations

import asyncio
import contextlib
import html as _html_mod
from typing import Any, Iterable

from loguru import logger

from app.config import get_settings

def _esc(text: str) -> str:
    return _html_mod.escape(str(text))

def required_chats() -> list[tuple[str, str]]:
    """Чаты, подписка хотя бы на один из которых даёт доступ: [(id, username)].

    В список всегда попадают и канал (CHANNEL_CHAT_ID / CHANNEL_USERNAME), и все
    отслеживаемые группы из TRACKED_CHAT_IDS: человек может состоять только в
    канале или только в группе обсуждения, и отказывать ему нельзя.
    """
    st = get_settings()
    seen: set[str] = set()
    chats: list[tuple[str, str]] = []

    def _add(value: object, uname: str = "") -> None:
        key = str(value or "").strip()
        if key and key not in seen:
            seen.add(key)
            chats.append((key, uname))

    _add(st.channel_chat_id, st.channel_username or "")
    for cid in st.tracked_chat_ids:
        _add(cid)
    return chats

def watched_chat_ids() -> set[int]:
    """Множество числовых id обязательных чатов (пусто = фильтра нет)."""
    ids: set[int] = set()
    for cid, _uname in required_chats():
        with contextlib.suppress(ValueError, TypeError):
            ids.add(int(str(cid)))
    return ids

def is_watched(chat_id: int | None) -> bool:
    if chat_id is None:
        return False
    ids = watched_chat_ids()
    return not ids or int(chat_id) in ids

def numeric_chat_id(target: str | int) -> int | None:
    """Числовой id чата из '-100...' / '123' / '@username' (None — если не id)."""
    s = str(target).lstrip("@")
    if s.startswith("-100"):
        s = s[4:]
    try:
        return int(s)
    except ValueError:
        return None

async def record_membership(user_id: int, chat_id: int | str | None = None, *,
                            first_name: str = "", username: str | None = None,
                            real_event: bool = True,
                            contacted: bool = False) -> None:
    """Единая точка входа всех сигналов членства и контакта в реестре.

    chat_id принимает и формат '-100...', и внутренний id Telethon. Идемпотентно,
    никогда не бросает наружу: ошибки БД логируются, но не влияют ни на выдачу
    доступа, ни на обработку апдейта.
    """
    cid = numeric_chat_id(chat_id) if chat_id is not None else None
    try:
        from app.db.repositories import SubscriberRepository
        from app.db.session import session_factory
        async with session_factory() as session:
            await SubscriberRepository(session).record_membership(
                int(user_id), cid, first_name=first_name, username=username,
                real_event=real_event, contacted=contacted)
        if real_event and cid is not None:
            logger.info("access: registry {} <- chat {} ({})", user_id, cid,
                        "event" if contacted is False else "event+contact")
    except Exception as exc:
        logger.warning("access: register {}@{} failed: {}: {}",
                       user_id, chat_id, type(exc).__name__, str(exc)[:200])

async def known_subscriber_ids(user_ids: Iterable[int]) -> set[int]:
    """Id из user_ids, у которых в реестре есть НЕПУСТОЙ список чатов.

    Непустой ``chats`` — единственное доказательство присутствия: строку без
    чатов создаёт и контакт с ботом (/start от неподписанного), поэтому она
    права доступа не даёт. Один запрос вместо N; ошибка БД — пустое множество.
    """
    ids = {int(u) for u in user_ids if u}
    if not ids:
        return set()
    try:
        from sqlalchemy import select

        from app.db.models import ChannelSubscriber
        from app.db.session import session_factory
        async with session_factory() as session:
            rows = (await session.execute(
                select(ChannelSubscriber.user_id, ChannelSubscriber.chats)
                .where(ChannelSubscriber.user_id.in_(ids)))).all()
        return {int(uid) for uid, chats in rows if list(chats or [])}
    except Exception as exc:
        logger.debug("access: registry read failed for {}: {}", user_ids, exc)
        return set()

async def remember_contact(user_id: int, *, first_name: str = "",
                           username: str | None = None) -> None:
    """Фиксирует контакт человека с ботом (ever_contacted).

    Сам по себе контакт ПРАВА ДОСТУПА не даёт: доступ доказывает только
    членство в обязательном чате. Нужно для статистики (/subscribers) и
    диагностики — видеть, кто реально писал боту.
    """
    try:
        from app.db.repositories import SubscriberRepository
        from app.db.session import session_factory
        async with session_factory() as session:
            await SubscriberRepository(session).record_membership(
                int(user_id), None, first_name=first_name, username=username,
                real_event=False, contacted=True)
    except Exception as exc:
        logger.warning("access: remember_contact {} failed: {}: {}",
                       user_id, type(exc).__name__, str(exc)[:200])

async def refresh_registry(bot, user_id: int) -> bool:
    """Живой скан участников всех обязательных чатов перед выдачей отказа.

    Совпадение имён с историческим API handlers.access.ensure_registry_fresh.
    """
    return await _refresh_registry_impl(bot, user_id)

async def known_subscriber(user_id: int) -> bool:
    """Человек числится в реестре с подтверждённым членством в одном из чатов."""
    return bool(await known_subscriber_ids([user_id]))

async def registry_state(user_id: int) -> str:
    """Текстовый снимок строки реестра для логов и /accessdebug."""
    try:
        from app.db.models import ChannelSubscriber
        from app.db.session import session_factory
        async with session_factory() as session:
            row = await session.get(ChannelSubscriber, int(user_id))
        if row is None:
            return "NO ROW"
        return f"row exists, chats={list(row.chats or [])!r}"
    except Exception as exc:
        return f"read failed: {type(exc).__name__}: {str(exc)[:80]}"

async def api_status_for(bot, user_id: int) -> tuple[dict[str, str], bool]:
    """Статусы пользователя по Bot API во всех обязательных чатах.

    Возвращает ({чат: статус}, был_ли_сбой). Сбоем считается любая ошибка
    TelegramForbiddenError/TelegramAPIError: отсутствие бота в чате, таймаут
    сети, некорректный id. Отрицательный ответ при сбое недоказуем.
    """
    statuses: dict[str, str] = {}
    errored = False
    for cid, uname in required_chats():
        target = f"@{uname}" if uname else cid
        try:
            member = await bot.get_chat_member(target, user_id)
            statuses[target] = str(getattr(member, "status", "") or "?")
        except Exception as exc:
            errored = True
            statuses[target] = f"error:{type(exc).__name__}"
    return statuses, errored

MEMBER_STATUSES = ("member", "administrator", "creator")

def has_membership(statuses: dict[str, str]) -> bool:
    """Хотя бы один чат ответил реальным членством по Bot API."""
    return any(s in MEMBER_STATUSES for s in statuses.values())

async def mtproto_status(target: str | int, user_id: int) -> tuple[str, str]:
    """MTProto-проба статуса: ('ok', статус) | ('silent', причина) | ('error', причина)."""
    try:
        from app.services.mtproto_client import get_chat_member_status
        status = await get_chat_member_status(target, user_id)
        if status is None:
            reason = ""
            with contextlib.suppress(Exception):
                from app.services.mtproto_client import last_scan_error
                reason = await last_scan_error()
            return ("silent", reason or "MTProto вернул None (нет клиента/прав/entity)")
        return ("ok", status)
    except Exception as exc:
        return ("error", f"{type(exc).__name__}: {str(exc)[:120]}")

async def scan_chat_participants(target: str) -> int:
    """Заносит всех участников одного чата в реестр через MTProto.

    Возвращает число записей; недоступный или настроенный иначе MTProto даёт 0
    без исключения. После скана сравнивает собранное с точным счётчиком чата и
    честно пишет WARNING при неполной выборке Telethon.
    """
    global LAST_SCAN_SEEN, LAST_SCAN_TOTAL
    from app.services.mtproto_sync import mtproto_configured

    if not mtproto_configured():
        return 0
    try:
        from app.services.mtproto_client import iter_all_participants
        members = await iter_all_participants(target)
    except Exception as exc:
        logger.debug("access: MTProto scan {} failed: {}", target, exc)
        return 0

    cid = numeric_chat_id(target)
    added = 0
    for m in members or []:
        try:
            uid = int(m["id"])
        except (KeyError, TypeError, ValueError):
            continue
        await record_membership(uid, cid, first_name=m.get("first_name") or "",
                                username=m.get("username"))
        added += 1

    LAST_SCAN_SEEN = len(members or [])
    total: int | None = None
    with contextlib.suppress(Exception):
        from app.services.mtproto_client import chat_participants_count
        total = await chat_participants_count(target)
    LAST_SCAN_TOTAL = total

    if added:
        logger.info("access: живой скан {} — {} участник(ов) в реестре",
                    target, added)
    if members and total and len(members) < max(2, total // 2):
        logger.error("access: ⚠️ скан {} собрал {} из ~{} участник(ов) — выборка "
                     "Telethon неполная; часть подписчиков осталась без записи "
                     "(проверьте показ списка участников в группе и права "
                     "MTProto-аккаунта)", target, len(members), total)
    return added

LAST_SCAN_SEEN: int | None = None
LAST_SCAN_TOTAL: int | None = None

_SCAN_COOLDOWN_SEC = 600
_scan_busy: set[str] = set()
_scan_last: dict[str, float] = {}

async def _refresh_registry_impl(bot, user_id: int) -> bool:
    """Обновляет реестр живым сканом участников (раз в SCAN_COOLDOWN на чат).

    Вызывается перед выдачей отказа: если человека нигде не видно, скан обязан
    дать ему шанс попасть в реестр и получить доступ сразу, а не после cron.
    True — хотя бы один скан реально запускался.
    """
    import time

    ran = False
    for cid, uname in required_chats():
        target = uname or cid
        key = str(target)
        now = time.monotonic()
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

async def last_scan_error_safe() -> str:
    """Причина молчания MTProto для диагностики (никогда не бросает)."""
    with contextlib.suppress(Exception):
        from app.services.mtproto_client import last_scan_error
        return await last_scan_error() or ""
    return ""

def spawn(coro: Any) -> None:
    """Fire-and-forget с сильной ссылкой на задачу (иначе GC может её собрать)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        return
    task = loop.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)

_BG_TASKS: set = set()
