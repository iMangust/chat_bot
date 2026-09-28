"""Опциональная синхронизация подписчиков через Telegram API (MTProto).

Зачем это нужно
---------------
Bot API принципиально НЕ отдаёт список участников канала и не шлёт событие
«человек подписался», если бот не админ с подпиской на chat_member. Поэтому
доступ к боту мог не появиться у тех, кто сам не «засветился»: не вступал
при запущенном боте, не писал сообщения и не нажимал /start.

Пользовательский аккаунт (Telethon) видит канал целиком: get_full_channel
возвращает участников даже без админ-прав. Этот модуль закрывает пробел:

    python -m app.services.mtproto_sync [--first-run]

собирает id участников обязательных чатов (gate.required_chats) и заносит их
в реестр доступа channel_subscribers. Гейт (middlewares/gate.py) пускает в
бот любого, кто есть в этом реестре, — без каких-либо рассылок.

Безопасность (почему это НЕ спам-рассылка):
* мы НИЧЕГО не отправляем с user-аккаунта — только читаем;
* аккаунт должен быть вторым («сервисным»), а не личным;
* включается только при заданных TELEGRAM_API_ID/HASH + MTPROTO_SESSION_STRING;
  без них вся функциональность бота работает как раньше (Bot API);
* после первичной синхронизации сессию лучше отозвать (Settings → Devices).

Секреты — только из .env (пример в .env.example), никогда не в коде/репо.
"""
from __future__ import annotations

import asyncio
import contextlib
import sys

from loguru import logger

from app.config import get_settings

async def collect_participant_ids() -> list[int]:
    """Список user_id участников всех обязательных чатов (без дублей).

    Требует telethon (устанавливается опционально: pip install telethon>=1.36).
    Клиент и авторизация — через app.services.mtproto_client (единственный
    источник конфигов; ключи читаются из .env, в коде их нет). Если ни
    чего нет — будет интерактивный логин по PHONE (не для продакшена).
    """
    from telethon.tl.functions.channels import GetFullChannelRequest

    from app.services.mtproto_client import holder, resolve_channel_entity

    if not holder.is_connected():
        await holder.get()

    from app.middlewares import gate as _gate_mod
    chats = _gate_mod.required_chats()
    if not chats:
        raise RuntimeError("required_chats пуст — нечего синхронизировать")

    ids: set[int] = set()
    client = await holder.get()
    for cid, uname in chats:
        target = uname or cid
        try:
            entity = await resolve_channel_entity(target)
        except Exception as exc:
            logger.error("MTProto: не удалось разрешить чат {!r}: {}"
                         " (укажите CHANNEL_USERNAME/public-ссылку)", target, exc)
            continue
        users: set[int] = set()
        total_count = None
        async def _collect() -> int:
            n = 0
            async for p in client.iter_participants(entity, aggressive=True):
                if not getattr(p, "bot", False):
                    users.add(int(p.id))
                    n += 1
            return n

        try:
            await _collect()
        except TypeError as exc:
            logger.debug("MTProto: iter_participants({}) TypeError: {} — "
                         "пробую get_participants()", target, exc)
            try:
                async for p in client.get_participants(entity):
                    if not getattr(p, "bot", False):
                        users.add(int(p.id))
            except Exception as exc2:
                logger.warning("MTProto: get_participants({}) failed: {}",
                               target, exc2)
        except Exception as exc:
            code = getattr(exc, "message", None) or str(exc)
            hint = ""
            if "PARTICIPANTS_TOO_LARGE" in str(code).upper():
                hint = (" — у ГРУППЫ выключен показ списка участников: включите "
                        "Настройки группы → «Показывать список участников» "
                        "(Telegram не отдаёт его даже админу MTProto)")
            logger.warning("MTProto: сбор участников {} не удался: {}{}",
                           target, code, hint)
        try:
            full = (await client(GetFullChannelRequest(entity))).full_chat
            total_count = getattr(full, "participants_count", None)
            for u in getattr(full, "participants", []) or []:
                if not getattr(u, "bot", False):
                    users.add(int(u.id))
        except Exception as exc:
            logger.debug("MTProto: get_full_channel({}) failed: {}", target, exc)
        if not users:
            logger.error(
                "MTProto: чат {} ({}) — 0 участников получено. Проверьте: "
                "(1) MTProto-аккаунт @{} состоит в ЭТОМ чате; (2) если это "
                "группа — в её настройках включено «Показывать список "
                "участников» (иначе Bot/MTProto без админ-прав его не видят); "
                "(3) у аккаунта есть права администратора канала.",
                target, cid, (holder.me.username if holder.me else "?"))
        else:
            logger.info("MTProto: {} — {} участник(ов) собрано (в чате всего {})",
                        target, len(users),
                        total_count if total_count is not None else "?")
        ids |= users
    return sorted(ids)

async def collect_participants_by_chat() -> dict[int, set[int]]:
    """Участники каждого обязательного чата отдельно: {chat_id: {user_id}}.

    v2.0.3: дельта-синку нужен разрез по чатам — сверять реестр надо «кто
    состоит в ЭТОМ чате», а не общим списком под первый чат (иначе участник
    группы получал членство в канале, и наоборот; см. sync_subscribers).
    Реализация тонкая: для каждого чата временно сужает список
    gate.required_chats до него одного и вызывает collect_participant_ids —
    одна точка сбора участников на весь модуль (Telethon-механика, логи и
    подсказки PARTICIPANTS_TOO_LARGE переиспользуются как есть).
    """
    from app.middlewares import gate as gate_mod

    chats = list(gate_mod.required_chats())
    out: dict[int, set[int]] = {}
    orig = gate_mod.required_chats
    try:
        for cid, uname in chats:
            try:
                chat_num = int(cid)
            except (TypeError, ValueError):
                continue
            gate_mod.required_chats = lambda _i=chat_num, _u=uname: [
                (str(_i), _u)]
            try:
                ids = await collect_participant_ids()
            except Exception as exc:
                logger.warning("MTProto: сбор участников {} сорвался: {}",
                               cid, str(exc)[:120])
                ids = []
            out[chat_num] = set(ids)
    finally:
        gate_mod.required_chats = orig
    return out

async def sync_subscribers(first_run: bool = False) -> dict:
    """Заносит участников каналов в реестр доступа channel_subscribers.

    v2.0.3 (боевые логи 22:25/23:41 — «подписан, но доступа нет»): раньше
    дельта-режим фильтровал по курсору «id > максимального известного»,
    считая Telegram-id монотонными метками свежести. Это ложь для старых
    аккаунтов: человек, состоящий в канале с момента его основания, имеет
    НИЗКИЙ id и никогда не проходил фильтр — cron-синки годами пропускали
    таких подписчиков, реестр оставался без их строк, а гейт (при скрытом
    списке участников Bot API отдаёт 'left') не находил человека ни в одном
    источнике ⇒ заглушка «🔒 подпишись» у реально подписанного пользователя.

    Теперь дельта-синк сверяет СПИСОК УЧАСТНИКОВ каждого чата с реестром
    напрямую: заносит всех, кого в реестре ещё нет (или кто числится, но без
    членства в этом чате). Идемпотентен, стоимость — один SELECT + апдейты
    только новых. first_run сохранён в сигнатуре для совместимости вызовов
    и теперь означает то же, что и обычный прогон (полная сверка).
    """
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.db.repositories import SubscriberRepository

    per_chat = await collect_participants_by_chat()
    st = get_settings()

    engine = create_async_engine(st.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    added = 0
    self_ids: set[int] = {await _self_bot_id()}
    with contextlib.suppress(Exception):
        from app.services.mtproto_client import holder
        me = holder.me
        if me is not None:
            self_ids.add(int(me.id))
    try:
        async with factory() as session:
            subs = SubscriberRepository(session)
            for chat_id, uids in per_chat.items():
                already = await subs.member_ids_by_chat(chat_id)
                for uid in sorted(uids - already):
                    if uid in self_ids:
                        continue
                    if await subs.add_if_new(uid, chat_id):
                        added += 1
            await session.commit()
    finally:
        await engine.dispose()
    total = sum(len(v) for v in per_chat.values())
    result = {"total": total, "added": added}
    logger.info("MTProto sync done: {} (проверено чатов: {})", result, len(per_chat))
    return result

async def full_rescan_subscribers() -> dict:
    """v1.5.73: полный автоматический скан ВСЕХ участников обязательных чатов.

    В отличие от дельта-режима (только id > курсора — «свежие регистрации»),
    здесь обходим весь список каждого чата и заносим в реестр доступа
    channel_subscribers каждого человека, которого ещё нет в базе — даже если
    он никогда не взаимодействовал с ботом. Заодно подтягиваем
    first_name/username для старых записей. С этого момента гейт пускает
    таких людей в бота (подписчик = доступ).

    Источник списка — MTProto (Telethon): Bot API принципиально не отдаёт
    участников канала. Приватность пользователей списку не мешает —
    GetParticipantsRequest видит и скрытых участников.
    """
    from app.db.models import ChannelSubscriber
    from app.db.repositories import SubscriberRepository
    from app.db.session import session_factory
    from app.middlewares.gate import required_chats
    from app.services.mtproto_client import holder, iter_all_participants

    if not mtproto_configured():
        return {"skipped": "mtproto not configured"}
    chats = required_chats()
    if not chats:
        return {"skipped": "no required chats"}

    self_ids: set[int] = {await _self_bot_id()}
    with contextlib.suppress(Exception):
        me = holder.me
        if me is not None:
            self_ids.add(int(me.id))

    added = updated = total_seen = 0
    failures: list[str] = []
    async with session_factory() as session:
        subs = SubscriberRepository(session)
        for cid, uname in chats:
            target = uname or cid
            members = await iter_all_participants(target)
            if not members:
                try:
                    from app.services.mtproto_client import last_scan_error
                    reason = await last_scan_error()
                except Exception:
                    reason = ""
                reason = reason or ("MTProto не вернул участников (аккаунт не в "
                                    "чате / PARTICIPANTS_TOO_LARGE / нет доступа)")
                failures.append(f"{target}: {reason}")
                logger.warning("MTProto rescan: чат {} — участников не получено: {}",
                               target, reason)
                continue
            total_seen += len(members)
            for m in members:
                uid = int(m["id"])
                if uid in self_ids:
                    continue
                row = await session.get(ChannelSubscriber, uid)
                if row is None:
                    if await subs.add_if_new(uid, cid,
                                             first_name=m.get("first_name") or "",
                                             username=m.get("username")):
                        added += 1
                else:
                    changed = False
                    if m.get("first_name") and row.first_name != m["first_name"]:
                        row.first_name = m["first_name"]
                        changed = True
                    if m.get("username") and row.username != m["username"]:
                        row.username = m["username"]
                        changed = True
                    if changed:
                        updated += 1
        await session.commit()
    result = {"seen": total_seen, "added": added, "updated": updated}
    if failures:
        result["errors"] = failures
    logger.info("📡 MTProto full rescan: {:n} участник(ов) просмотрено, "
                "{} новых в реестр доступа, {} обновлено{}", total_seen, added,
                updated,
                f" (ошибок чатов: {len(failures)})" if failures else "")
    return result

async def _self_bot_id() -> int:
    """id основного бота — он не «подписчик», в реестре доступа ему не место."""
    tok = get_settings().bot_token
    try:
        return int(tok.split(":")[0])
    except (ValueError, IndexError):
        return -1

def mtproto_configured() -> bool:
    """Можно ли вообще запускать MTProto-синк (ключи + чем авторизоваться)."""
    from app.services.mtproto_client import credentials_configured, telethon_available
    return telethon_available() and credentials_configured()

async def autosync_if_configured(first_run: bool | None = None) -> dict | None:
    """Синк при старте бота: полная синхронизация при первой загрузке базы.

    first_run=None → автоопределение: база подписчиков пуста (или это первый
    прогон после v1.5.11) ⇒ тянем ВСЕХ участников канала разом; иначе дельту.
    Возвращает None, если MTProto не настроен (бот живёт только на Bot API).
    """
    if not mtproto_configured():
        return None
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.db.repositories import SubscriberRepository
    from app.db.session import engine
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        known = await SubscriberRepository(session).count()
    full = (first_run if first_run is not None else known == 0)
    logger.info("MTProto autosync: режим {} (база: {} подписчик(ов))",
                "ПОЛНАЯ" if full else "дельта", known)
    return await sync_subscribers(first_run=full)

async def login_and_print_session_string() -> str:
    """Интерактивный логин (--login): телефон → код → 2FA.

    Создаёт *.session рядом с cwd и печатает MTPROTO_SESSION_STRING для
    безинтерактивного деплоя. Секреты в лог не пишутся.
    """
    from app.services.mtproto_client import holder
    client = await holder.get()
    string = await client.session.save_to_string()
    me = holder.me
    print("\n✅ Логин успешен:", me.id, me.username or "")
    print("Для сервера добавьте в .env:")
    print(f"MTPROTO_SESSION_STRING={string[:8]}…(полная строка ниже)")
    print(string)
    return string

async def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--login" in argv:
        try:
            await login_and_print_session_string()
        except Exception as exc:
            logger.error("MTProto login failed: {}", exc)
            return 1
        finally:
            from app.services.mtproto_client import holder
            await holder.disconnect()
        return 0
    first_run = "--first-run" in argv
    try:
        res = await sync_subscribers(first_run=first_run)
    except Exception as exc:
        logger.error("MTProto sync failed: {}", exc)
        return 1
    finally:
        from app.services.mtproto_client import holder
        await holder.disconnect()
    print(f"Участников: {res['total']}, новых в реестре доступа: {res['added']}")
    return 0

if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
