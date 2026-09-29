"""Единый MTProto-клиент (Telethon) для всего «полного Telegram API».. Назначение:
* один переиспользуемый клиент на процесс (подключение + keepalive);
* конфигурирование из Settings (креды приходят ТОЛЬКО из.env / окружения —
  в коде никаких реальных ключей, см..env.example с серыми примерами);
* ленивая инициализация: без api_id/api_hash модуль импортопригоден, а
  вызовы дают понятную ошибку вместо падения бота;
* режимы стринг-сессии Telethon ('BQ...') и файла *.session.

Используется:
* app.services.mtproto_sync  — чтение списка участников канала (get_full_channel);
* app.services.userbot       — ответы от user-аккаунта (режимы user/hybrid);
* app.handlers.admin         — команды /mtproto, /syncnow.

ВАЖНО: это user-аккаунт. Флуд/рассылки с него запрещены правилами Telegram;
используйте второй (service) аккаунт, не личный.
"""
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
    try:
        import telethon
        _TELETHON_IMPORT_ERROR = ""
        return True
    except ImportError:
        _TELETHON_IMPORT_ERROR = "no"
        return False

def credentials_configured() -> bool:
    """Есть ли api_id+api_hash (минимум для подключения при готовой сессии)."""
    st = get_settings()
    return bool(st.telegram_api_id and st.telegram_api_hash)

def session_configured() -> bool:
    """Есть ли чем авторизоваться: строковая сессия или существующий файл."""
    from pathlib import Path
    st = get_settings()
    if st.mtproto_session_string:
        return True
    p = Path(st.mtproto_session or "")
    return p.with_suffix(".session").is_file() or p.is_file()

class MtprotoClientHolder:
    """Ленинный синглтон Telethon-клиента."""

    def __init__(self) -> None:
        self._client: Any | None = None
        self._lock = asyncio.Lock()
        self._me: Any | None = None

    async def _authorized(self) -> bool:
        """Telethon: is_user_authorized — КОРУТИНА (await обязателен).

        Синхронный вызов давал RuntimeWarning «coroutine was never awaited»
        и всегда True (bool coroutine-объекта), из-за чего использовался
        отключённый клиент.: проверяем is_connected синхронно,
        авторизацию — только через await.
        """
        c = self._client
        if c is None or not getattr(c, "is_connected", lambda: False)():
            return False
        try:
            return bool(await c.is_user_authorized())
        except Exception:
            return False

    async def get(self) -> Any:
        """Подключённый клиент либо RuntimeError с внятной подсказкой."""
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
            client = TelegramClient(session, st.telegram_api_id,
                                    st.telegram_api_hash)
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
            logger.info("MTProto: подключено (user id={} @{})",
                        me.id, me.username or "-")
            return client

    @property
    def me(self) -> Any | None:
        return self._me

    def is_connected(self) -> bool:
        """Синхронная проверка «клиент жив».

        ВАЖНО: у Telethon is_user_authorized — КОРУТИНА. Её синхронный вызов
        всегда возвращает coroutine-объект (truthy) и печатает RuntimeWarning
        «coroutine was never awaited» (лог). Здесь проверяем только
        фактическое соединение; авторизацию делает await _authorized.
        """
        c = self._client
        return bool(c is not None and getattr(c, "is_connected", lambda: False)())

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:
                logger.debug("MTProto disconnect: {}", exc)
            self._client = None
            self._me = None

holder = MtprotoClientHolder()

_DIALOGS_CACHE: dict[tuple[str, int], Any] = {}
_DIALOGS_CACHE_TS: float = 0.0

def _inner_id(chat_id: int | str) -> int:
    """Bot API id (-100xxxxxxxxxx) → внутренний id канала (Telethon)."""
    s = str(chat_id)
    return int(s[4:]) if s.startswith("-100") else int(s)

def _participants_filter():
    """Совместимый со всеми Telethon фильтр «все участники» .

    Telethon <=1.41: ChannelParticipantsSearch(name=""). В 1.42+ поле `name`
    убрали — конструирование с ним кидало TypeError, из-за чего падала ВСЯ
    MTProto-проверка участника в гейте доступа (регрессия; в логах:
    «member status... failed»). Перебираем сигнатуры и берём первую
    работающую; в самом крайнем случае — Recent (последние по активности,
    участник почти всегда внутри выборки).
    """
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
    """Максимальная страница выборки участников (лимит API — 200)."""
    return 200

_LAST_SCAN_ERROR: str = ""

async def last_scan_error() -> str:
    """Последняя диагностика сбора участников (для честных логов/команды)."""
    return _LAST_SCAN_ERROR or ""

def _peer_inner_id(entity: Any) -> int | None:
    """Внутренний id канала/группы из Peer-объекта entity.peer."""
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
    """Точное число участников чата глазами MTProto (GetFullChannel/GetFullChat).

    Используется диагностикой гейта: позволяет отличить «скан молча собрал
    меньше половины чата» от «список реально полный». Возвращает None, если
    узнать число не удалось (клиент не в чате, нет прав, ошибка сети) —
    caller тогда делает вывод только по собранным данным. НИКОГДА не бросает.
    """
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.messages import GetFullChatRequest

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
        logger.debug("MTProto: chat_participants_count({}) failed: {}",
                     chat_id, str(exc)[:120])
        return None

async def iter_all_participants(chat_id: int | str):
    """Все НЕ-боты-участники чата глазами MTProto-аккаунта .

    Возвращает list[dict]: {"id", "first_name", "username"} — включая людей со
    включённой приватностью (скрытый список участников). Пустой список =>
    аккаунт не в чате / канал недоступен / PARTICIPANTS_TOO_LARGE / ошибка
    (наружу НЕ бросаем — caller решает сам; причина — в last_scan_error).: раньше использовался сырой GetParticipantsRequest(offset=N) — при
    фильтре «все» Telegram разрешает пагинацию только до offset≤~10000, а без
    точного access_hash канала запрос и вовсе падает (в логах: rescan давал
    «0 участник(ов)», хотя дельта-синк тех же людей видел). Теперь основной
    путь — Telethon client.get_participants(entity): он сам передаёт хэши
    участников между страницами и ходит бесконечно долго; сырые страницы —
    только запасной путь для unit-тестов с моками.
    """
    global _LAST_SCAN_ERROR
    _LAST_SCAN_ERROR = ""
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
        """Собрать участников: список (await) OR async-итератор."""
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
            logger.debug("MTProto: participants-вызов #%d TypeError: %s",
                         attempt, str(exc)[:120])
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
        logger.debug("MTProto: iter_all_participants({}) failed: {}",
                     chat_id, str(exc)[:150])
    return out

async def get_chat_member_status(chat_id: int | str, user_id: int) -> str | None:
    """Статус участника глазами MTProto-аккаунта — фолбэк для Bot API.. Bot API (getChatMember) принципиально не видит «анонимных»
    участников: при включённой приватности («Keep Groups Private» / скрытый
    список участников) состоящий в чате человек отдаётся как left/kicked, и
    гейт ложно блокирует настоящих подписчиков. Userbot-аккаунт, состоящий в
    чате, через GetParticipantsRequest видит таких пользователей честно.

    Возвращает 'member'/'administrator'/'creator'/'left'/None;
    None => MTProto не настроен/не в чате/ошибка — caller решает сам
    (обычно fail-open по базе подписчиков). НИКОГДА не бросает наружу.

    v2.0.1 (лог «участники не могут взаимодействовать»): раньше проба делала
    ОДНУ страницу GetParticipantsRequest(offset=0, limit=N). При фильтре
    ChannelParticipantsSearch("") Telegram сортирует выдачу по релевантности
    поиска, а не по дате вступления, и человек мог просто не попасть в первые
    N записей — проба возвращала None, гейт блокировал реального подписчика.
    Теперь используется iter_all_participants (полный обход с пагинацией и
    жонглированием access_hash, тот же путь, что у MTProto-синков), сырая
    одностраничная проба осталась только запасным путём.
    """
    try:
        from telethon import functions, types

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
        except Exception:
            pass
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
        logger.debug("MTProto member status {}→{} failed: {}", chat_id, user_id, exc)
        return None

async def resolve_channel_entity(target: str | int) -> Any:
    """Entity канала/группы по username или внутреннему id (-100...).

    Для числовых id без access_hash GetFullChannel требует точное разрешение;
    пробуем username/@ссылку, затем ищем среди диалогов аккаунта.: поддержка малых групп (PeerChat). Ранее искали только Channel —
    для обычных групповых чатов (id без префикса -100) поиск всегда давал
    RuntimeError («prime... failed: RuntimeError» в логах), и baseline
    снапшотов реакций не строился ни для одного группового чата. Теперь:
      • -100… → ищем PeerChannel/Channel с внутренним id;
      • просто отрицательный id (группа) → Chat с этим id;
      • положительный «голый» id канала → тоже пробуем как внутренний.
    Диалоги кэшируются на 60 с (iter_dialogs — дорогой запрос).
    """
    from telethon.tl.types import Channel, Chat, PeerChannel, PeerChat
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
        try:
            from telethon.tl.types import InputChannel as _IC
            from telethon.tl.functions.channels import (
                GetChannelsRequest as _GetChannels,
            )
            from telethon.errors import ChannelPrivateError
            resp = await client(_GetChannels(
                [_IC(channel_id=want_channel_inner, access_hash=0)]))
            for ent in getattr(resp, "chats", []) or []:
                if isinstance(ent, Channel) and int(ent.id) == want_channel_inner:
                    _DIALOGS_CACHE[("c", want_channel_inner)] = ent
        except ChannelPrivateError:
            raise RuntimeError(f"Чат {target}: аккаунт не имеет доступа "
                               "(приватный канал?)")
        except Exception:
            pass
        ent = _DIALOGS_CACHE.get(("c", want_channel_inner))
        if ent is not None:
            return ent
    raise RuntimeError(
        f"Чат {target} не найден среди диалогов MTProto-аккаунта — "
        "добавьте аккаунт в чат или используйте CHANNEL_USERNAME")
