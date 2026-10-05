from __future__ import annotations

import asyncio
import contextlib
import sys

from loguru import logger

from app.config import get_settings

async def collect_participant_ids() -> list[int]:
    from telethon.tl.functions.channels import GetFullChannelRequest

    from app.services.mtproto_client import holder, resolve_channel_entity

    if not holder.is_connected():
        await holder.get()

    from app.middlewares import gate as _gate_mod
    chats = _gate_mod.serviceable_chats()
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
    from app.middlewares import gate as gate_mod

    chats = list(gate_mod.serviceable_chats())
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

async def _bot_api_confirmed_members(chat_id: int, uids: set[int]) -> set[int]:
    if not uids:
        return set()
    token = get_settings().bot_token
    if not token:
        return uids
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.exceptions import TelegramAPIError
    bot = Bot(token, session=AiohttpSession())
    out: set[int] = set()
    try:
        targets = [chat_id]
        try:
            uname = (get_settings().channel_username or "").strip().lstrip("@")
            if uname and f"@{uname}" not in targets:
                targets.append(f"@{uname}")
        except Exception as exc:
            logger.debug("MTProto sync: channel_username lookup failed: {}", exc)
        for uid in uids:
            confirmed = False
            chat_not_found = False
            for target in targets:
                try:
                    member = await bot.get_chat_member(target, uid)
                    if str(getattr(member, "status", "") or "") in (
                            "member", "administrator", "creator"):
                        confirmed = True
                        break
                except TelegramAPIError as exc:
                    if "chat not found" in str(exc).lower():
                        chat_not_found = True
                    continue
                except Exception as exc:
                    logger.debug("MTProto sync: Bot API verify {} in {} failed: {}",
                                 uid, target, type(exc).__name__)
                    continue
            if chat_not_found and not confirmed:
                logger.warning(
                    "MTProto sync: chat {} not visible to the bot — "
                    "treating all candidates as NOT members (config error)",
                    chat_id)
                break
            if confirmed:
                out.add(uid)
    finally:
        try:
            await bot.session.close()
        except Exception as exc:
            logger.debug("MTProto sync: bot session close failed: {}", exc)
    return out

async def sync_subscribers(first_run: bool = False) -> dict:
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
                new_ids = uids - already
                if new_ids:
                    new_ids = await _bot_api_confirmed_members(chat_id, new_ids)
                for uid in sorted(new_ids):
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
    from app.db.models import ChannelSubscriber
    from app.db.repositories import SubscriberRepository
    from app.db.session import session_factory
    from app.middlewares.gate import serviceable_chats
    from app.services.mtproto_client import holder, iter_all_participants

    if not mtproto_configured():
        return {"skipped": "mtproto not configured"}
    chats = serviceable_chats()
    if not chats:
        return {"skipped": "no required chats"}

    self_ids: set[int] = {await _self_bot_id()}
    with contextlib.suppress(Exception):
        me = holder.me
        if me is not None:
            self_ids.add(int(me.id))

    added = updated = total_seen = 0
    failures: list[str] = []
    api_verified: dict[int, set[int]] = {}
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
            new_uids = {int(m["id"]) for m in members
                        if int(m["id"]) not in self_ids}
            existing_rows = set()
            async with session_factory() as s0:
                from sqlalchemy import select as _select
                from app.db.models import ChannelSubscriber as _CS
                rows0 = (await s0.execute(
                    _select(_CS.user_id, _CS.chats))).all()
                for uid0, chats0 in rows0:
                    if int(uid0) in new_uids:
                        existing_rows.add(int(uid0))
            to_verify = {u for u in new_uids if u not in existing_rows}
            api_verified[int(cid)] = await _bot_api_confirmed_members(
                int(cid), to_verify) if to_verify else set()
            for m in members:
                uid = int(m["id"])
                if uid in self_ids:
                    continue
                row = await session.get(ChannelSubscriber, uid)
                if row is None:
                    if uid not in api_verified[int(cid)]:
                        continue
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
    tok = get_settings().bot_token
    try:
        return int(tok.split(":")[0])
    except (ValueError, IndexError):
        return -1

def mtproto_configured() -> bool:
    from app.services.mtproto_client import credentials_configured, telethon_available
    return telethon_available() and credentials_configured()

async def autosync_if_configured(first_run: bool | None = None) -> dict | None:
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
