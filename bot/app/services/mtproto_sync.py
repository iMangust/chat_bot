"""Опциональная синхронизация подписчиков через Telegram API (MTProto).

Зачем это нужно
---------------
Bot API принципиально НЕ отдаёт список участников канала и не шлёт событие
«человек подписался», если бот не админ с подпиской на chat_member. Поэтому
приветствие доходит только до тех, кто сам «засветился»: вступил при
запущенном боте, написал сообщение или нажал /start.

Пользовательский аккаунт (Telethon) видит канал целиком: get_full_channel
возвращает участников даже без админ-прав. Этот модуль использует второй бот
(service-аккаунт) как РАЗОВЫЙ инструмент:

    python -m app.services.mtproto_sync [--first-run]

собирает id участников обязательных чатов (gate.required_chats), заносит их в
channel_subscribers (pending) и выходит. Дальше стандартный механизм
(welcome_pending_subscribers — скан, старт, события) рассылает приветствия по
своим правилам: ровно одно DM на пользователя, welcomed_at-дедупликация,
проверка membership через Bot API.

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
        await holder.get()  # понятная RuntimeError-подсказка, если не настроено

    from app.middlewares.gate import required_chats
    chats = required_chats()
    if not chats:
        raise RuntimeError("required_chats пуст — нечего синхронизировать")

    ids: set[int] = set()
    client = await holder.get()
    for cid, uname in chats:
        target = uname or cid
        try:
            entity = await resolve_channel_entity(target)
        except Exception as exc:  # noqa: BLE001 — wrong_type/username_not_occupied и т.п.
            logger.error("MTProto: не удалось разрешить чат {!r}: {}"
                         " (укажите CHANNEL_USERNAME/public-ссылку)", target, exc)
            continue
        users: set[int] = set()
        total_count = None
        # v1.5.17: ОСНОВНОЙ источник участников — асинхронный обход
        # iter_participants()/get_participants() (Telethon возвращает один и тот
        # же _ParticipantsIter; в раннинге event loop используется __aiter__).
        # Причины прошлых провалов:
        #  * iter_participants(..., request_size=200) — Telethon НЕ принимает
        #    request_size (лог: «got an unexpected keyword argument»), исключение
        #    глоталось и синк давал 0 при живом канале;
        #  * GetFullChannel отдаёт лишь выборку из 0-21 id — только на него
        #    полагаться нельзя.
        # Для группы со СКРЫТЫМ списком участников сервер ответит
        # PARTICIPANTS_TOO_LARGE даже админу — это отдельная строка в логе:
        # лечится включением «Показывать список участников» в настройках группы.
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
            # совместимость на случай иной сигнатуры в других версиях Telethon
            logger.debug("MTProto: iter_participants({}) TypeError: {} — "
                         "пробую get_participants()", target, exc)
            try:
                async for p in client.get_participants(entity):
                    if not getattr(p, "bot", False):
                        users.add(int(p.id))
            except Exception as exc2:  # noqa: BLE001
                logger.warning("MTProto: get_participants({}) failed: {}",
                               target, exc2)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "message", None) or str(exc)
            hint = ""
            if "PARTICIPANTS_TOO_LARGE" in str(code).upper():
                hint = (" — у ГРУППЫ выключен показ списка участников: включите "
                        "Настройки группы → «Показывать список участников» "
                        "(Telegram не отдаёт его даже админу MTProto)")
            logger.warning("MTProto: сбор участников {} не удался: {}{}",
                           target, code, hint)
        # GetFullChannel — как дополнение: точный счётчик + кэш id (ускоряет)
        try:
            full = (await client(GetFullChannelRequest(entity))).full_chat
            total_count = getattr(full, "participants_count", None)
            for u in getattr(full, "participants", []) or []:
                if not getattr(u, "bot", False):
                    users.add(int(u.id))
        except Exception as exc:  # noqa: BLE001
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


async def sync_subscribers(first_run: bool = False) -> dict:
    """Заносит участников каналов в channel_subscribers (pending-welcome).

    first_run=False (дефолт, режим «дельты»): добавляются ТОЛЬКО пользователи
    с id больше максимального известного (Telegram-id монотонны ⇒ это свежие
    регистрации). Так периодический cron не затирает welcome-очередь старыми
    участниками и не создаёт иллюзию «новых».
    first_run=True: заносятся ВСЕ участники (первичная загрузка базы).
    """
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.db.repositories import SubscriberRepository

    ids = await collect_participant_ids()
    st = get_settings()
    tracked = st.tracked_chat_ids
    default_chat = st.channel_chat_id or (tracked[0] if tracked else 0)

    engine = create_async_engine(st.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    added = skipped = 0
    self_ids: set[int] = {await _self_bot_id()}
    # v1.5.18: MTProto-аккаунт (он же может быть админом/владельцем чатов)
    # физически присутствует в каналах — его нельзя заносить в welcome-очередь,
    # иначе бот начнёт слать приветствие самому себе при каждом дельта-синке.
    with contextlib.suppress(Exception):
        from app.services.mtproto_client import holder
        me = holder.me  # кэшируется при подключении (свойство, без await)
        if me is not None:
            self_ids.add(int(me.id))
    try:
        async with factory() as session:
            subs = SubscriberRepository(session)
            cursor = None if first_run else await subs.last_seen_user_id()
            for uid in ids:
                if uid in self_ids:
                    continue
                if cursor is not None and uid <= cursor:
                    skipped += 1
                    continue
                if await subs.add_if_new(uid, default_chat):
                    added += 1
            # v1.5.16: после полной синхронизации сбрасываем welcome-очередь
            # до размера «сколько реально добавили» — иначе первый прогон на
            # канале в N человек оставит висящие pending-записи навсегда, и
            # они будут рефлешиться косвенными сигналами.
            await session.commit()
    finally:
        await engine.dispose()
    result = {"total": len(ids), "added": added, "skipped_old": skipped}
    logger.info("MTProto sync done: {}", result)
    return result


async def dispatch_welcomes(limit: int = 20) -> int:
    """v1.5.16: раздать приветствия из очереди своим собственным Bot-сессионным
    контекстом — НЕ зависит от того, инициализирован ли aiogram-Bot в текущем
    task'е (планировщик/CLI). Возвращает число отправленных DM."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from app.db.session import session_factory
    from app.handlers.welcome import welcome_pending_subscribers
    st = get_settings()
    bot = Bot(token=st.bot_token,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        async with session_factory() as session:
            return await welcome_pending_subscribers(bot, session, limit=limit)
    finally:
        await bot.session.close()


async def _self_bot_id() -> int:
    """id основного бота — его не надо приветствовать самого себя."""
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
    st = get_settings()
    if not st.welcome_channel_enabled or not mtproto_configured():
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
    client = await holder.get()          # сам запросит phone/code/password
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
        except Exception as exc:  # noqa: BLE001
            logger.error("MTProto login failed: {}", exc)
            return 1
        finally:
            from app.services.mtproto_client import holder
            await holder.disconnect()
        return 0
    first_run = "--first-run" in argv
    try:
        res = await sync_subscribers(first_run=first_run)
    except Exception as exc:  # noqa: BLE001
        logger.error("MTProto sync failed: {}", exc)
        return 1
    finally:
        from app.services.mtproto_client import holder
        await holder.disconnect()
    print(f"Участников: {res['total']}, новых в базе: {res['added']}, "
          f"старых пропущено: {res['skipped_old']}")
    print("Приветствия разошлются штатным механизмом (скан каждые "
          f"{get_settings().channel_scan_minutes} мин / старт бота).")
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
