from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from app.config import get_settings

_TELETHON_IMPORT_ERROR: str | None = None

def telethon_available() -> bool:
    global _TELETHON_IMPORT_ERROR
    if _TELETHON_IMPORT_ERROR is not None:
        return _TELETHON_IMPORT_ERROR == ""
    import importlib.util
    if importlib.util.find_spec("telethon") is None:
        _TELETHON_IMPORT_ERROR = "no"
        return False
    _TELETHON_IMPORT_ERROR = ""
    return True

def credentials_configured() -> bool:
    st = get_settings()
    return bool(st.telegram_api_id and st.telegram_api_hash)

def session_configured() -> bool:
    from pathlib import Path
    st = get_settings()
    if st.mtproto_session_string:
        return True
    p = Path(st.mtproto_session or "")
    return p.with_suffix(".session").is_file() or p.is_file()

class MtprotoClientHolder:

    def __init__(self) -> None:
        self._client: Any | None = None
        self._lock = asyncio.Lock()
        self._me: Any | None = None

    async def _authorized(self) -> bool:
        c = self._client
        if c is None or not getattr(c, "is_connected", lambda: False)():
            return False
        try:
            return bool(await c.is_user_authorized())
        except Exception:
            return False

    async def get(self) -> Any:
        if await self._authorized():
            return self._client
        async with self._lock:
            if await self._authorized():
                return self._client
            if not telethon_available():
                raise RuntimeError(
                    "telethon не установлен: pip install 'telethon>=1.36'")
            st = get_settings()
            if not credentials_configured():
                raise RuntimeError(
                    "MTProto не настроен: укажите в .env TELEGRAM_API_ID "
                    "(или API_ID) и TELEGRAM_API_HASH (API_HASH) — "
                    "https://my.telegram.org → API development tools")
            from telethon import TelegramClient
            session = st.mtproto_session_string or st.mtproto_session or "mtproto_sync"
            # connection_retries=0: Telethon по умолчанию делает 10 внутренних
            # повторов на каждый запрос. При рассинхронизации pts сервер
            # отвечает PersistentTimestampOutdatedError, и эти повторы печатают
            # пачки «Telegram is having internal issues» в лог (по записи на
            # каждый повтор). Самовосстановление pts происходит через штатный
            # catch-up (get_difference), а периодический delta-sync запускается
            # планировщиком заново — внутренние повторы только плодят шум.
            client = TelegramClient(session, st.telegram_api_id,
                                    st.telegram_api_hash,
                                    request_retries=3, connection_retries=0)
            if st.mtproto_session_string:
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    raise RuntimeError(
                        "MTPROTO_SESSION_STRING неавторизованна/истекла — "
                        "перегенерируйте: python -m app.services.mtproto_sync --login")
            else:
                if not st.telegram_phone:
                    await client.connect()
                    if not await client.is_user_authorized():
                        await client.disconnect()
                        raise RuntimeError(
                            "Сессия не авторизована. Варианты: интерактивный "
                            "логин `python -m app.services.mtproto_sync --login` "
                            "(одноразово, создаёт .session или печатает "
                            "SESSION_STRING) либо TELEGRAM_PHONE/PHONE в .env")
                password = st.telegram_password or None
                await client.start(
                    phone=st.telegram_phone or None,
                    password=(lambda: password) if password else None)
            me = await client.get_me()
            self._client = client
            self._me = me
            logger.info(f"MTProto: подключено (user id={me.id} @{me.username or '-'})")
            return client

    @property
    def me(self) -> Any | None:
        return self._me

    def is_connected(self) -> bool:
        c = self._client
        return bool(c is not None and getattr(c, "is_connected", lambda: False)())

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:
                logger.debug(f"MTProto disconnect: {exc}")
            self._client = None
            self._me = None

holder = MtprotoClientHolder()

_DIALOGS_CACHE: dict[tuple[str, int], Any] = {}
_DIALOGS_CACHE_TS: float = 0.0

def _inner_id(chat_id: int | str) -> int:
    s = str(chat_id).strip().lstrip("@")
    if s.startswith("-100"):
        return int(s[4:])
    try:
        return int(s)
    except ValueError:
        raise ValueError(f"chat id must be numeric or -100..., got {chat_id!r}") from None

def _participants_filter():
    import inspect

    from telethon import types

    cls = types.ChannelParticipantsSearch
    params = inspect.signature(cls.__init__).parameters
    kwargs = {}
    for field in ("query", "name"):
        if field in params:
            kwargs = {field: ""}
            break
    try:
        return cls(**kwargs)
    except Exception:
        return types.ChannelParticipantsRecent()

def _participants_limit() -> int:
    return 200

_LAST_SCAN_ERROR: str = ""

async def last_scan_error() -> str:
    return _LAST_SCAN_ERROR or ""

def _peer_inner_id(entity: Any) -> int | None:
    peer = getattr(entity, "peer", None)
    for attr in ("channel_id", "chat_id"):
        val = getattr(peer, attr, None)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                return None
    return None

async def chat_participants_count(chat_id: int | str) -> int | None:
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.messages import GetFullChatRequest

        chat_id = _resolve_numeric_target(chat_id)
        client = await holder.get()
        inner = _inner_id(chat_id)
        try:
            entity = await resolve_channel_entity(chat_id)
        except Exception:
            entity = None
        if entity is None:
            return None
        peers = _peer_inner_id(entity)
        s = str(chat_id).strip()
        is_small_group = (not s.startswith("-100")) and s.startswith("-")
        full = None
        try:
            if is_small_group:
                full = await client(GetFullChatRequest(peers or inner))
            else:
                full = await client(GetFullChannelRequest(entity))
        except Exception:
            try:
                full = await client(GetFullChannelRequest(entity))
            except Exception:
                return None
        counts = getattr(full, "counts", None)
        total = getattr(counts, "participants_count", None)
        if total is None:
            for path in (("chat_full", "participants_count"), ("full_chat", None)):
                obj = getattr(full, path[0], None)
                cand = getattr(obj, "participants_count", None) if obj is not None else None
                if cand is not None:
                    total = cand
                    break
        return int(total) if total is not None else None
    except Exception as exc:
        logger.debug(f"MTProto: chat_participants_count({chat_id}) failed: {str(exc)[:120]}")
        return None

async def iter_all_participants(chat_id: int | str):
    global _LAST_SCAN_ERROR
    _LAST_SCAN_ERROR = ""
    chat_id = _resolve_numeric_target(chat_id)
    try:
        client = await holder.get()
    except Exception as exc:
        _LAST_SCAN_ERROR = f"MTProto-клиент недоступен: {str(exc)[:120]}"
        return []
    entity = None
    try:
        entity = await resolve_channel_entity(chat_id)
    except Exception as exc:
        _LAST_SCAN_ERROR = f"не удалось разрешить чат {chat_id!r}: {str(exc)[:120]}"
        s = str(chat_id).strip()
        if not (s.startswith("-") or s.isdigit()):
            try:
                entity = await client.get_entity(s if s.startswith("@") else f"@{s}")
                _LAST_SCAN_ERROR = ""
            except Exception:
                entity = None
    if entity is None:
        if not _LAST_SCAN_ERROR:
            _LAST_SCAN_ERROR = (f"чат {chat_id!r} не найден среди диалогов "
                                "MTProto-аккаунта (он в нём не состоит?)")
        return []

    out: list[dict] = []
    seen_ids: set[int] = set()

    def _add(u) -> None:
        try:
            uid = int(u.id)
        except Exception:
            return
        if getattr(u, "bot", False) or uid in seen_ids:
            return
        seen_ids.add(uid)
        out.append({"id": uid,
                    "first_name": getattr(u, "first_name", "") or "",
                    "username": getattr(u, "username", None)})

    collected = False

    async def _collect(coro_or_iter) -> None:
        obj = coro_or_iter
        if hasattr(obj, "__await__") and not hasattr(obj, "__aiter__"):
            obj = await obj
        if hasattr(obj, "__aiter__"):
            async for u in obj:
                _add(u)
        else:
            for u in obj:
                _add(u)

    for attempt, factory in enumerate((
            lambda: client.iter_participants(entity, aggressive=True),
            lambda: client.get_participants(entity, aggressive=True),
            lambda: client.get_participants(entity))):
        try:
            await _collect(factory())
            collected = True
            break
        except TypeError as exc:
            logger.debug(f"MTProto: participants-вызов #{attempt} "
                         f"TypeError: {str(exc)[:120]}")
        except Exception as exc:
            code = str(getattr(exc, "message", None) or exc)
            if "PARTICIPANTS_TOO_LARGE" in code.upper():
                _LAST_SCAN_ERROR = ("PARTICIPANTS_TOO_LARGE: у группы выключен "
                                    "показ списка участников — включите "
                                    "Настройки группы → «Показывать список "
                                    "участников» (Telegram не отдаёт его даже "
                                    "админу)")
            elif "ACCESS_HASH" in code.upper() or "INVALID" in code.upper():
                _LAST_SCAN_ERROR = (f"сбой доступа к списку участников: "
                                    f"{code[:120]} (переподключите MTProto-сессию)")
            else:
                _LAST_SCAN_ERROR = f"get_participants: {code[:120]}"
    if collected:
        _LAST_SCAN_ERROR = ""
        return out

    try:
        from telethon import functions, types

        inner = _inner_id(chat_id)
        chan = entity if not isinstance(entity, (int, str)) else None
        input_chan = None
        if chan is not None and hasattr(chan, "access_hash"):
            input_chan = types.InputChannel(channel_id=inner,
                                            access_hash=chan.access_hash or 0)
        if input_chan is None:
            return out
        flt = _participants_filter()
        page_size = _participants_limit()
        offset = 0
        total = None
        while True:
            resp = await client(functions.channels.GetParticipantsRequest(
                channel=input_chan, filter=flt, offset=offset,
                limit=page_size, hash=0))
            if total is None:
                total = int(getattr(resp, "count", 0) or 0)
            users = {int(u.id): u for u in getattr(resp, "users", []) or []}
            got = 0
            for p in getattr(resp, "participants", []) or []:
                pid = getattr(getattr(p, "peer", None), "user_id", None)
                if pid is None:
                    continue
                pid = int(pid)
                got += 1
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                u = users.get(pid)
                if u is None or getattr(u, "bot", False):
                    continue
                out.append({"id": pid,
                            "first_name": getattr(u, "first_name", "") or "",
                            "username": getattr(u, "username", None)})
            offset += page_size
            if got == 0 or offset >= total:
                break
        _LAST_SCAN_ERROR = ""
    except Exception as exc:
        if not _LAST_SCAN_ERROR:
            _LAST_SCAN_ERROR = f"GetParticipantsRequest: {str(exc)[:120]}"
        logger.debug(f"MTProto: iter_all_participants({chat_id}) failed: {str(exc)[:150]}")
    return out

def _resolve_numeric_target(chat_id: int | str) -> Any:
    s = str(chat_id).strip().lstrip("@")
    if not s or (s[0] != "-" and not s.isdigit()):
        return chat_id
    try:
        _inner_id(s)
    except ValueError:
        return chat_id
    return f"-100{s}" if s.startswith("-") and not s.startswith("-100") else s


async def get_chat_member_status(chat_id: int | str, user_id: int) -> str | None:
    try:
        from telethon import functions, types

        chat_id = _resolve_numeric_target(chat_id)
        client = await holder.get()
        inner = _inner_id(chat_id)

        people = await iter_all_participants(chat_id)
        if people:
            for p in people:
                try:
                    if int(p["id"]) == int(user_id):
                        return "member"
                except (KeyError, TypeError, ValueError):
                    continue
            return "left"

        entity = None
        access_hash = 0
        try:
            ent = _DIALOGS_CACHE.get(("c", inner))
            if ent is None:
                await resolve_channel_entity(chat_id)
                ent = _DIALOGS_CACHE.get(("c", inner))
            if ent is not None:
                entity = ent
                access_hash = getattr(ent, "access_hash", 0) or 0
        except Exception as exc:
            logger.debug(f"MTProto: cached channel entity lookup failed: {exc}")
        if entity is None:
            s = str(chat_id).strip()
            if s.startswith("-100") or s.isdigit():
                entity = types.InputChannel(channel_id=inner, access_hash=access_hash)
            else:
                entity = await client.get_entity(s if s.startswith("@") else f"@{s}")
        resp = await client(functions.channels.GetParticipantsRequest(
            channel=entity,
            filter=_participants_filter(),
            offset=0,
            limit=_participants_limit(),
            hash=0,
        ))
        users = {int(u.id): u for u in getattr(resp, "users", []) or []}
        for p in getattr(resp, "participants", []) or []:
            pid = getattr(getattr(p, "peer", None), "user_id", None)
            if pid is None or int(pid) != int(user_id):
                continue
            cls = type(p).__name__
            if cls == "ChannelParticipantAdmin":
                return "administrator"
            if cls == "ChannelParticipantCreator":
                return "creator"
            if cls == "ChannelParticipantSelf":
                return "left"
            if cls == "ChannelParticipantBanned":
                left = bool(getattr(p, "left", False))
                if not left:
                    return "member"
                return "left"
            if cls == "ChannelParticipant":
                return "member"
            return "left"
        if int(user_id) in users:
            return "member"
        return "left"
    except Exception as exc:
        logger.debug(f"MTProto member status {chat_id}→{user_id} failed: {exc}")
        return None

async def resolve_channel_entity(target: str | int) -> Any:
    from telethon.tl.types import Channel, Chat
    client = await holder.get()
    s = str(target).strip()
    want_channel_inner: int | None = None
    want_chat_id: int | None = None
    if s.startswith("-100"):
        want_channel_inner = int(s[4:])
    elif s.startswith("-"):
        want_chat_id = int(s[1:])
    elif s.isdigit():
        want_channel_inner = int(s)
    else:
        return await client.get_entity(s if s.startswith("@") else f"@{s}")

    global _DIALOGS_CACHE_TS
    import time as _t
    now_m = _t.monotonic()
    if now_m - _DIALOGS_CACHE_TS > 60 or not _DIALOGS_CACHE:
        new_cache: dict = {}
        async for d in client.iter_dialogs():
            ent = d.entity
            if isinstance(ent, Channel):
                new_cache[("c", int(ent.id))] = ent
            elif isinstance(ent, Chat):
                new_cache[("g", int(ent.id))] = ent
        _DIALOGS_CACHE.clear()
        _DIALOGS_CACHE.update(new_cache)
        _DIALOGS_CACHE_TS = now_m
    if want_channel_inner is not None:
        ent = _DIALOGS_CACHE.get(("c", want_channel_inner))
        if ent is not None:
            return ent
    if want_chat_id is not None:
        ent = _DIALOGS_CACHE.get(("g", want_chat_id))
        if ent is not None:
            return ent
    if want_channel_inner is not None:
        # Fallback для каналов вне списка диалогов (аккаунт давно не активен —
        # канал «выпадает» из dialogs). GetChannels с access_hash=0 работает
        # только если TL-кэш Telethon уже знает хеш; иначе сервер отвечает
        # CHANNEL_INVALID / "Invalid channel object". Тогда прогреваем кэш
        # через get_input_entity по @username канала и повторяем попытку.
        from telethon.errors import ChannelPrivateError
        from telethon.tl.functions.channels import (
            GetChannelsRequest as _GetChannels,
        )
        from telethon.tl.types import InputChannel as _IC

        async def _try_getchannels() -> bool:
            resp = await client(_GetChannels(
                [_IC(channel_id=want_channel_inner, access_hash=0)]))
            for ent in getattr(resp, "chats", []) or []:
                if isinstance(ent, Channel) and int(ent.id) == want_channel_inner:
                    _DIALOGS_CACHE[("c", want_channel_inner)] = ent
                    return True
            return False

        try:
            if await _try_getchannels():
                return _DIALOGS_CACHE[("c", want_channel_inner)]
        except ChannelPrivateError as err:
            raise RuntimeError(f"Чат {target}: аккаунт не имеет доступа "
                               "(приватный канал?)") from err
        except Exception as exc:
            logger.debug(f"MTProto: GetChannels fallback for {target} failed: {exc}")
        uname = get_settings().channel_username
        if uname:
            try:
                await client.get_input_entity(str(uname).lstrip("@"))
                if await _try_getchannels():
                    return _DIALOGS_CACHE[("c", want_channel_inner)]
            except Exception as exc:
                logger.debug(
                    f"MTProto: GetChannels retry after entity warm-up "
                    f"({uname}) for {target} failed: {type(exc).__name__}")
        ent = _DIALOGS_CACHE.get(("c", want_channel_inner))
        if ent is not None:
            return ent
    raise RuntimeError(
        f"Чат {target} не найден среди диалогов MTProto-аккаунта — "
        "добавьте аккаунт в чат или используйте CHANNEL_USERNAME")
