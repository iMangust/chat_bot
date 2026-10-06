"""REST API данных бота для веб-панели: пользователи, питомцы, достижения,
мерч, мероприятия, подписчики, статистика.

Все ручки работают напрямую с БД (session_factory) и не требуют запущенного
бота — панель остаётся управляющей даже при остановленном инстансе.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select

router = APIRouter(prefix="/api")


def _dt(v: Any) -> str | None:
    if v is None:
        return None
    try:
        # Панель смотрят на Камчатке: моменты из БД (UTC или старые naive-
        # локальные метки) приводим к местному времени перед выводом.
        from app.utils.local_time import localize
        return localize(v).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(v)


def _enum(v: Any) -> str:
    return str(v.value if hasattr(v, "value") else v)


# ---- русские подписи enum-значений для панели ----
SPECIES_RU = {"cat": "Котёнок", "dog": "Щенок", "fox": "Лисёнок",
              "chinchilla": "Шиншилла", "owl": "Совёнок", "dragon": "Дракончик"}
STAGE_RU = {"egg": "🥚 Яйцо", "baby": "🐣 Малыш", "teen": "🐱 Подросток",
            "adult": "😼 Взрослый", "legendary": "🐲 Легендарный"}
CATEGORY_RU = {"activity": "💬 Активность", "streak": "🔥 Серия",
               "reactions": "👍 Реакции", "pet": "🐾 Питомец",
               "social": "🤝 Социальные", "secret": "🙈 Секретные"}
RARITY_RU = {"common": "Обычное", "rare": "Редкое", "epic": "Эпическое",
             "legendary": "Легендарное"}
COND_RU = {"pet_created": "Питомец заведён", "messages_total": "Сообщений всего",
           "messages_day": "Сообщений за день", "streak_days": "Серия дней",
           "reactions_given": "Реакций дано", "reactions_received": "Реакций получено",
           "pet_feeds": "Кормлений питомца", "pet_level": "Уровень питомца",
           "pet_walks": "Прогулок питомца", "invites": "Приглашено друзей",
           "top1_day": "1 место в топе дня", "level": "Уровень игрока",
           "coins_earned": "Монет заработано", "games_won": "Игр выиграно"}
STAT_RU = {"messages_day_total": "Сообщений за день", "pet_feeds": "Кормлений",
           "pet_walks": "Прогулок питомца", "reactions_given": "Реакций дано",
           "reactions_received": "Реакций получено", "invites": "Приглашений",
           "top1_day": "Побед в топе дня", "coins_earned": "Монет заработано",
           "games_won": "Игр выиграно", "pet_level": "Макс. уровень питомца"}


def _ru(mapping: dict[str, str], key: Any) -> str:
    """Русская подпись enum-значения; если ключ неизвестен — как есть."""
    k = str(key.value if hasattr(key, "value") else key)
    return mapping.get(k, k)


def _session():
    # async_sessionmaker сам является async-контекстным менеджером;
    # раньше здесь был "return session_factory()" + "async with await ...",
    # что ломало все ручки API (AttributeError на входе в сессию).
    from app.db.session import session_factory
    return session_factory()


# ============================ контроль доступа ============================
# Панель может слушать 0.0.0.0 (удалённый доступ), поэтому все ручки данных
# закрыты токеном: без него — 401. Фронтенд получает его при загрузке страницы
# (/api/token, доступен только localhost/allowlist) и подставляет в заголовок
# X-Dashboard-Token.
#
# Безопасность (issue #4): BOT_TOKEN больше НЕ принимается как токен панели —
# утечка ключа доступа к API Telegram давала бы полный доступ к персональным
# данным пользователей в БД. Разрешены только явные секреты панели:
#   1) DASHBOARD_TOKEN (приоритет);
#   2) WEBHOOK_SECRET_TOKEN — только если задан и отличается от дефолтного.
# Если ни один не задан, панель генерирует ephemeral-токен на запуске
# (см. app.web.server.lifespan) — он выдаётся фронту через
# /api/token, поэтому локальная панель работает «из коробки», но секрет
# нигде не хранится в открытом виде и исчезает после рестарта.

DEFAULT_WEBHOOK_SECRET = "change-me-in-env"

# Эфемерный токен, сгенерированный при старте панели (см. server.py).
_ephemeral_token: str = ""


def set_ephemeral_token(value: str) -> None:
    """Установить сгенерированный при старте ephemeral-токен панели."""
    global _ephemeral_token
    _ephemeral_token = value or ""


def _api_tokens() -> set[str]:
    """Допустимые токены панели (BOT_TOKEN намеренно исключён).

    Приоритет: DASHBOARD_TOKEN (явный токен панели) > WEBHOOK_SECRET_TOKEN
    (если задан и не дефолтный) > ephemeral-токен, сгенерированный при старте.

    ВАЖНО: читаем .env НАПРЯМУЮ, а не только кэшированные get_settings():
    settings кешируются lru_cache при первом импорте модуля, и если процесс
    панели стартовал без загруженного .env (или переменные поменяли во вкладке
    «Конфигурация» без рестарта), токен из настроек не совпадал с фактическим —
    /api/token отдавал пустоту, и все вкладки данных получали 401
    («информация не выводится»).
    """
    toks = set()
    env_vals: dict[str, str] = {}
    try:
        from app.web.server import _env_value  # резолвер файла .env
        for key in ("DASHBOARD_TOKEN", "WEBHOOK_SECRET_TOKEN"):
            v = _env_value(key)
            if v:
                env_vals[key] = v
    except Exception as exc:
        logger.debug("admin API: .env token lookup failed: %s", exc)
    try:
        from app.config import get_settings
        settings = get_settings()
        dash = str(getattr(settings, "dashboard_token", "") or "")
        sec = str(getattr(settings, "webhook_secret_token", "") or "")
    except Exception:
        dash = sec = ""
    dash = env_vals.get("DASHBOARD_TOKEN") or dash
    sec = env_vals.get("WEBHOOK_SECRET_TOKEN") or sec
    if dash:
        toks.add(dash)
    if sec and sec != DEFAULT_WEBHOOK_SECRET:
        toks.add(sec)
    if _ephemeral_token:
        toks.add(_ephemeral_token)
    return toks


def _db_ready() -> bool:
    """Есть ли уже инициализированный session_factory (БД поднята)."""
    try:
        from app.db import session as dbs
        return getattr(dbs, "session_factory", None) is not None
    except Exception:
        return False


_db_init_lock = asyncio.Lock()
_db_init_done = False


async def _ensure_db() -> None:
    """Гарантировать, что таблицы существуют перед чтением из БД.

    Движок/сессии поднимаются импортом app.db.session (engine создаётся при
    импорте модуля), но DDL-миграции (create_all + лёгкие миграции колонок/
    таблиц) живут в on_startup бота. Панель может быть открыта сразу после
    деплоя, когда эти миграции ещё ни разу не выполнялись (или их надо
    повторить после пересоздания базы). При первом же запросе к API запускаем
    их сами — идемпотентно и один раз на процесс. Это лечит «вкладки пустые /
    ничего не выводится» (500 на несуществующих таблицах).
    """
    global _db_init_done
    if _db_init_done:
        return
    async with _db_init_lock:
        if _db_init_done:
            return
        try:
            from app.db.session import engine
            from app.main import _light_migrations, ensure_events_table
            async with engine.begin() as conn:
                from app.db.models import Base
                await conn.run_sync(Base.metadata.create_all)
                await _light_migrations(conn)
            await ensure_events_table(engine)
            _db_init_done = True
            logger.info("admin API: схема БД проверена/создана при первом запросе")
        except Exception as exc:  # pragma: no cover
            logger.warning("admin API: ленивая инициализация схемы не удалась: %r",
                           exc)


def require_token(token: str | None) -> None:
    # сравнение через secrets.compare_digest — устойчиво к time-атакам
    import secrets as _secrets
    toks = _api_tokens()
    if not token or not any(_secrets.compare_digest(token, t) for t in toks):
        raise HTTPException(401, "нет доступа: требуется токен панели")


async def _tok(x: str | None = Header(default=None, alias="X-Dashboard-Token")) -> None:
    require_token(x)
    # до чтения любых данных убедимся, что схема на месте
    await _ensure_db()


@router.get("/token")
async def dashboard_token(request: Request) -> dict:
    """Раздать токен фронту панели.

    Доступно только с localhost напрямую либо через сам интерфейс панели
    (запрос прошёл её IP-allowlist middleware — ставит request.state.allowed).
    """
    peer = request.client.host if request.client else ""
    local = peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost")
    via_panel = getattr(request.state, "allowed", False)
    if not (local or via_panel):
        raise HTTPException(403, "токен выдаётся только интерфейсу панели")
    toks = sorted(_api_tokens())
    return {"token": toks[0] if toks else ""}


# ============================ общая статистика ============================

@router.get("/stats/overview", dependencies=[Depends(_tok)])
async def stats_overview() -> dict:
    from app.db.models import (
        Achievement,
        ChannelSubscriber,
        Event,
        Pet,
        User,
        UserAchievement,
        utcnow,
    )
    async with _session() as s:
        users_total = (await s.execute(select(func.count(User.tg_id)))).scalar() or 0
        users_week = (await s.execute(
            select(func.count(User.tg_id)).where(
                User.created_at >= utcnow() - timedelta(days=7)))).scalar() or 0
        banned = (await s.execute(
            select(func.count(User.tg_id)).where(User.is_banned == True))).scalar() or 0
        pets_total = (await s.execute(select(func.count(Pet.id)).where(
            Pet.is_archived == False))).scalar() or 0
        ach_total = (await s.execute(select(func.count(Achievement.id)))).scalar() or 0
        unlocks = (await s.execute(select(func.count(UserAchievement.id)).where(
            UserAchievement.unlocked_at.is_not(None)))).scalar() or 0
        subs = (await s.execute(select(func.count(ChannelSubscriber.user_id)))).scalar() or 0
        events = (await s.execute(select(func.count(Event.id)))).scalar() or 0
        top = (await s.execute(
            select(User).order_by(User.xp.desc()).limit(5))).scalars().all()
    return {
        "usersTotal": users_total, "usersWeek": users_week, "banned": banned,
        "petsTotal": pets_total, "achievements": ach_total, "unlocks": unlocks,
        "subscribers": subs, "events": events,
        "topUsers": [{"tgId": u.tg_id, "name": u.first_name or u.username or str(u.tg_id),
                      "level": u.level, "xp": u.xp, "coins": u.coins} for u in top],
    }


# ================================ пользователи ================================

@router.get("/users", dependencies=[Depends(_tok)])
async def list_users(q: str = "", limit: int = Query(50, le=500),
                     offset: int = 0, sort: str = "created",
                     banned: str = "") -> dict:
    from app.db.models import User
    stmt = select(User)
    if q.strip():
        qq = q.strip()
        conds = [User.username.ilike(f"%{qq}%"), User.first_name.ilike(f"%{qq}%")]
        if qq.lstrip("-").isdigit():
            conds.append(User.tg_id == int(qq))
        stmt = stmt.where(or_(*conds))
    if banned == "yes":
        stmt = stmt.where(User.is_banned == True)
    elif banned == "no":
        stmt = stmt.where(User.is_banned == False)
    order = {"created": User.created_at.desc(), "xp": User.xp.desc(),
             "level": User.level.desc(), "coins": User.coins.desc(),
             "active": User.updated_at.desc()}.get(sort, User.created_at.desc())
    async with _session() as s:
        total = (await s.execute(
            select(func.count()).select_from(stmt.subquery()))).scalar() or 0
        rows = (await s.execute(stmt.order_by(order).limit(limit).offset(offset))
                ).scalars().all()
    items = [{
        "tgId": u.tg_id, "username": u.username, "firstName": u.first_name,
        "level": u.level, "xp": u.xp, "coins": u.coins,
        "streak": u.streak_days, "bestStreak": u.best_streak,
        "messages": u.messages_count, "onboarded": u.onboarded,
        "banned": u.is_banned, "petName": u.pet_name,
        "createdAt": _dt(u.created_at), "updatedAt": _dt(u.updated_at),
    } for u in rows]
    return {"total": total, "items": items}


@router.get("/users/{tg_id}", dependencies=[Depends(_tok)])
async def user_detail(tg_id: int) -> dict:
    """Карточка пользователя. Все необязательные блоки считаются защищённо:
    одна отсутствующая таблица/битая связь не должна ронять ручку в 500."""
    from app.db.models import NotificationSetting, Pet, ReactionLog, User, UserStat
    try:
        async with _session() as s:
            u = (await s.execute(select(User).where(User.tg_id == tg_id))
                 ).scalar_one_or_none()
            if u is None:
                raise HTTPException(404, f"пользователь {tg_id} не найден")
            user_info = {
                "tgId": u.tg_id, "username": u.username, "firstName": u.first_name,
                "lastName": u.last_name, "lang": u.lang, "level": u.level,
                "xp": u.xp, "coins": u.coins, "streak": u.streak_days,
                "bestStreak": u.best_streak, "messages": u.messages_count,
                "reactionsGiven": u.reactions_given,
                "reactionsReceived": u.reactions_received,
                "onboarded": u.onboarded, "welcomeShown": u.welcome_shown,
                "banned": u.is_banned, "referrerId": u.referrer_id,
                "createdAt": _dt(u.created_at), "updatedAt": _dt(u.updated_at),
                "lastActiveDate": _dt(u.last_active_date),
            }
            pet = (await s.execute(select(Pet).where(Pet.user_id == tg_id)
                                   .order_by(Pet.is_archived, Pet.born_at.desc()))
                   ).scalars().first()
            pet_info = ({
                "id": pet.id, "name": pet.name, "species": _enum(pet.species),
                "speciesRu": _ru(SPECIES_RU, pet.species),
                "stage": _enum(pet.stage), "stageRu": _ru(STAGE_RU, pet.stage),
                "level": pet.level, "xp": pet.xp,
                "hunger": round(float(pet.hunger), 1),
                "happiness": round(float(pet.happiness), 1),
                "energy": round(float(pet.energy), 1),
                "hygiene": round(float(pet.hygiene), 1),
                "health": round(float(pet.health), 1),
                "strength": pet.strength, "agility": pet.agility,
                "intellect": pet.intellect,
                "sleeping": bool(pet.is_sleeping), "archived": bool(pet.is_archived),
                "generation": pet.generation, "bornAt": _dt(pet.born_at),
                "lastUpdate": _dt(pet.last_update),
            } if pet else None)
            notif = (await s.execute(select(NotificationSetting).where(
                NotificationSetting.user_id == tg_id))).scalars().first()
            notif_info = ({
                "petReminders": notif.pet_reminders,
                "streakReminders": notif.streak_reminders,
                "achievementNotifications": notif.achievement_notifications,
                "dailyReport": notif.daily_report,
            } if notif else None)
            counters = {
                "reactionsGiven": (await s.execute(select(func.count()).select_from(
                    ReactionLog).where(ReactionLog.from_user == tg_id))).scalar() or 0,
                "reactionsReceived": (await s.execute(select(func.count()).select_from(
                    ReactionLog).where(ReactionLog.to_user == tg_id))).scalar() or 0,
                "invited": (await s.execute(select(func.count()).select_from(User).where(
                    User.referrer_id == tg_id))).scalar() or 0,
            }
            stat_rows = (await s.execute(
                select(UserStat).where(UserStat.user_id == tg_id))).scalars().all()
            stats = {st.key: st.value for st in stat_rows}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("user_detail failed for %s", tg_id)
        raise HTTPException(500, f"Ошибка карточки пользователя: {type(exc).__name__}: {exc}") from exc

    # Достижения — отдельным защищённым блоком: при проблеме с таблицей
    # user_achievements карточка всё равно открывается.
    achievements: list[dict] = []
    try:
        from app.db.models import Achievement, UserAchievement
        async with _session() as s:
            rows = (await s.execute(
                select(UserAchievement, Achievement)
                .join(Achievement, Achievement.id == UserAchievement.achievement_id)
                .where(UserAchievement.user_id == tg_id))).all()
            achievements = [{
                "achievementId": ua.achievement_id, "code": a.code,
                "title": a.title, "icon": a.icon, "description": a.description,
                "category": _enum(a.category), "categoryRu": _ru(CATEGORY_RU, a.category),
                "rarity": _enum(a.rarity), "rarityRu": _ru(RARITY_RU, a.rarity),
                "progress": ua.progress, "conditionValue": a.condition_value,
                "unlockedAt": _dt(ua.unlocked_at),
            } for ua, a in rows]
    except Exception:
        logger.exception("user_detail: блок достижений недоступен для %s", tg_id)

    return {
        "user": user_info,
        "pet": pet_info,
        "achievements": achievements,
        "stats": stats,
        "statLabels": STAT_RU,
        "notifications": notif_info,
        "counters": counters,
    }


class BanPatch(BaseModel):
    banned: bool


@router.post("/users/{tg_id}/ban", dependencies=[Depends(_tok)])
async def set_ban(tg_id: int, body: BanPatch) -> dict:
    from app.db.models import User
    async with _session() as s:
        u = (await s.execute(select(User).where(User.tg_id == tg_id))
             ).scalar_one_or_none()
        if u is None:
            raise HTTPException(404, f"пользователь {tg_id} не найден")
        u.is_banned = body.banned
        await s.commit()
    return {"ok": True, "tgId": tg_id, "banned": body.banned}


class UserEdit(BaseModel):
    coins: int | None = None
    xp: int | None = None
    level: int | None = None
    messages_count: int | None = None
    streak_days: int | None = None


@router.post("/users/{tg_id}/edit", dependencies=[Depends(_tok)])
async def edit_user(tg_id: int, body: UserEdit) -> dict:
    from app.db.models import User
    async with _session() as s:
        u = (await s.execute(select(User).where(User.tg_id == tg_id))
             ).scalar_one_or_none()
        if u is None:
            raise HTTPException(404, f"пользователь {tg_id} не найден")
        changed = []
        if body.coins is not None:
            u.coins = max(0, int(body.coins)); changed.append("coins")
        if body.xp is not None:
            u.xp = max(0, int(body.xp)); changed.append("xp")
        if body.level is not None:
            u.level = max(1, int(body.level)); changed.append("level")
        if body.messages_count is not None:
            u.messages_count = max(0, int(body.messages_count)); changed.append("messages_count")
        if body.streak_days is not None:
            u.streak_days = max(0, int(body.streak_days))
            u.best_streak = max(u.best_streak, u.streak_days)
            changed.append("streak_days")
        await s.commit()
    return {"ok": True, "changed": changed}


# ================================ питомцы ================================

@router.get("/pets/{pet_id}", dependencies=[Depends(_tok)])
async def pet_detail(pet_id: int) -> dict:
    """Карточка питомца + инвентарь (для редактора в web-панели)."""
    from app.db.models import Item, Pet, PetInventory, User
    async with _session() as s:
        p = (await s.execute(select(Pet).where(Pet.id == pet_id))
             ).scalar_one_or_none()
        if p is None:
            raise HTTPException(404, "питомец не найден")
        owner = (await s.execute(select(User).where(User.tg_id == p.user_id))
                 ).scalar_one_or_none()
        rows = (await s.execute(
            select(PetInventory, Item).join(Item, Item.id == PetInventory.item_id)
            .where(PetInventory.pet_id == pet_id)
            .order_by(Item.type, Item.name))).all()
    return {
        "pet": {
            "id": p.id, "userId": p.user_id,
            "owner": (owner.first_name or owner.username or str(p.user_id)) if owner else "?",
            "name": p.name, "species": _enum(p.species),
            "speciesRu": _ru(SPECIES_RU, p.species),
            "stage": _enum(p.stage), "stageRu": _ru(STAGE_RU, p.stage),
            "level": p.level, "xp": p.xp,
            "hunger": round(float(p.hunger)), "happiness": round(float(p.happiness)),
            "energy": round(float(p.energy)), "hygiene": round(float(p.hygiene)),
            "health": round(float(p.health)),
            "strength": p.strength, "agility": p.agility, "intellect": p.intellect,
            "sleeping": p.is_sleeping, "archived": bool(getattr(p, "is_archived", False)),
            "generation": getattr(p, "generation", 1),
            "bornAt": _dt(getattr(p, "born_at", None)),
            "lastUpdate": _dt(p.last_update),
        },
        "inventory": [{
            "invId": inv.id, "itemId": inv.item_id, "code": it.code, "name": it.name,
            "icon": it.icon, "type": it.type, "price": it.price,
            "quantity": inv.quantity,
        } for inv, it in rows],
    }

@router.get("/pets", dependencies=[Depends(_tok)])
async def list_pets(q: str = "", limit: int = Query(50, le=500), offset: int = 0) -> dict:
    from app.db.models import Pet, User
    stmt = select(Pet, User).join(User, User.tg_id == Pet.user_id)
    if q.strip():
        qq = f"%{q.strip()}%"
        stmt = stmt.where(or_(Pet.name.ilike(qq), User.username.ilike(qq),
                              User.first_name.ilike(qq)))
    async with _session() as s:
        total = (await s.execute(
            select(func.count()).select_from(stmt.subquery()))).scalar() or 0
        rows = (await s.execute(stmt.order_by(Pet.level.desc(), Pet.xp.desc())
                                .limit(limit).offset(offset))).all()
    items = []
    for pet, user in rows:
        items.append({
            "id": pet.id, "userId": pet.user_id,
            "owner": user.first_name or user.username or str(user.tg_id),
            "name": pet.name, "species": _enum(pet.species),
            "speciesRu": _ru(SPECIES_RU, pet.species),
            "stage": _enum(pet.stage), "stageRu": _ru(STAGE_RU, pet.stage),
            "level": pet.level, "xp": pet.xp, "hunger": round(pet.hunger, 1),
            "happiness": round(pet.happiness, 1), "energy": round(pet.energy, 1),
            "hygiene": round(pet.hygiene, 1), "health": round(pet.health, 1),
            "sleeping": pet.is_sleeping, "archived": pet.is_archived,
            "bornAt": _dt(pet.born_at), "lastUpdate": _dt(pet.last_update),
        })
    return {"total": total, "items": items}


# ================================ достижения ================================

@router.get("/achievements", dependencies=[Depends(_tok)])
async def achievements_list(with_holders: bool = False, user_id: int = 0) -> dict:
    from app.db.models import Achievement, UserAchievement
    async with _session() as s:
        achs = (await s.execute(select(Achievement).order_by(
            Achievement.category, Achievement.id))).scalars().all()
        counts: dict[int, int] = {}
        if with_holders:
            rows = (await s.execute(
                select(UserAchievement.achievement_id, func.count())
                .where(UserAchievement.unlocked_at.is_not(None))
                .group_by(UserAchievement.achievement_id))).all()
            counts = dict(rows)
        ustate: dict[int, tuple] = {}
        if user_id:
            rows = (await s.execute(select(UserAchievement).where(
                UserAchievement.user_id == user_id))).scalars().all()
            ustate = {r.achievement_id: (r.unlocked_at is not None, r.progress) for r in rows}
    return {"items": [{
        "id": a.id, "code": a.code, "title": a.title, "description": a.description,
        "icon": a.icon, "category": _enum(a.category),
        "categoryRu": _ru(CATEGORY_RU, a.category),
        "rarity": _enum(a.rarity), "rarityRu": _ru(RARITY_RU, a.rarity),
        "conditionType": _enum(a.condition_type),
        "conditionRu": _ru(COND_RU, a.condition_type),
        "conditionValue": a.condition_value, "rewardXp": a.reward_xp,
        "rewardCoins": a.reward_coins, "hidden": a.is_hidden,
        "holders": counts.get(a.id, 0) if with_holders else None,
        "userHas": bool(ustate.get(a.id, (False, 0))[0]) if user_id else None,
        "userProgress": ustate.get(a.id, (False, 0))[1] if user_id else None,
    } for a in achs]}


@router.get("/achievements/{ach_id}/holders", dependencies=[Depends(_tok)])
async def achievement_holders(ach_id: int, limit: int = Query(100, le=500)) -> dict:
    """Кто владеет достижением (для вкладки «Достижения»)."""
    from app.db.models import Achievement, User, UserAchievement
    async with _session() as s:
        a = await s.get(Achievement, ach_id)
        if a is None:
            raise HTTPException(404, "достижение не найдено")
        rows = (await s.execute(
            select(UserAchievement, User)
            .join(User, User.tg_id == UserAchievement.user_id)
            .where(UserAchievement.achievement_id == ach_id,
                   UserAchievement.unlocked_at.is_not(None))
            .order_by(UserAchievement.unlocked_at.desc()).limit(limit))).all()
    return {"achievement": {"id": a.id, "code": a.code, "title": a.title,
                           "icon": a.icon},
            "items": [{"userId": u.tg_id,
                       "name": u.first_name or u.username or str(u.tg_id),
                       "username": u.username, "level": u.level,
                       "unlockedAt": _dt(ua.unlocked_at)} for ua, u in rows]}


# ================================ мерч ================================

@router.get("/merch", dependencies=[Depends(_tok)])
async def merch_list() -> dict:
    from app.db.models import MerchCategory, MerchProduct, MerchVariant
    async with _session() as s:
        cats = (await s.execute(select(MerchCategory).order_by(
            MerchCategory.position, MerchCategory.id))).scalars().all()
        prods = (await s.execute(select(MerchProduct))).scalars().all()
        vars_ = (await s.execute(select(MerchVariant))).scalars().all()
    by_prod: dict[int, list] = {}
    for v in vars_:
        by_prod.setdefault(v.product_id, []).append({
            "id": v.id, "size": v.size, "color": v.color, "priceRub": v.price_rub,
            "stock": v.stock, "reservedBy": v.reserved_by, "soldCount": v.sold_count,
        })
    cat_items = []
    for c in cats:
        pcats = [p for p in prods if p.category_id == c.id]
        cat_items.append({
            "id": c.id, "code": c.code, "title": c.title, "icon": c.icon,
            "position": c.position,
            "products": [{
                "id": p.id, "name": p.name, "description": p.description,
                "imageUrl": p.image_url, "sizes": p.sizes or [],
                "colors": p.colors or [], "variants": by_prod.get(p.id, []),
            } for p in pcats],
        })
    return {"categories": cat_items}


class MerchCategoryBody(BaseModel):
    code: str = ""          # необязателен: сгенерируем автоматически (catN)
    title: str
    icon: str = "🧢"
    position: int = 0


class MerchProductBody(BaseModel):
    category_id: int
    name: str
    description: str = ""
    image_url: str | None = None


class MerchVariantBody(BaseModel):
    product_id: int
    size: str = ""
    color: str = ""
    price_rub: int = 0
    stock: int = 0


class MerchVariantPatch(BaseModel):
    price_rub: int | None = None
    stock: int | None = None
    size: str | None = None
    color: str | None = None


@router.post("/merch/categories", dependencies=[Depends(_tok)])
async def merch_add_category(body: MerchCategoryBody) -> dict:
    from app.db.models import MerchCategory
    from app.db.repositories import MerchRepository
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(422, "название категории обязательно")
    code = (body.code or "").strip().lower().replace(" ", "_")
    async with _session() as s:
        if not code:
            # код не обязателен для админа — генерируем уникальный catN
            max_id = (await s.execute(select(func.max(MerchCategory.id)))).scalar() or 0
            for cand_id in range(max_id + 1, max_id + 1001):
                cand = f"cat{cand_id}"
                exists = (await s.execute(select(MerchCategory.id).where(
                    MerchCategory.code == cand))).first()
                if not exists:
                    code = cand
                    break
        else:
            exists = (await s.execute(select(MerchCategory.id).where(
                MerchCategory.code == code))).first()
            if exists:
                raise HTTPException(422, f"код «{code}» уже занят")
        try:
            c = await MerchRepository(s).add_category(
                code, title, body.icon or "🧢", body.position)
            await s.commit()
        except Exception as exc:
            await s.rollback()
            raise HTTPException(422, f"не удалось добавить категорию: {exc}") from exc
    return {"ok": True, "id": getattr(c, "id", None), "code": code}


@router.delete("/merch/categories/{code}", dependencies=[Depends(_tok)])
async def merch_del_category(code: str) -> dict:
    from app.db.repositories import MerchRepository
    async with _session() as s:
        ok = await MerchRepository(s).delete_category(code)
        await s.commit()
    if not ok:
        raise HTTPException(404, "категория не найдена")
    return {"ok": True}


@router.post("/merch/products", dependencies=[Depends(_tok)])
async def merch_add_product(body: MerchProductBody) -> dict:
    from app.db.repositories import MerchRepository
    async with _session() as s:
        try:
            p = await MerchRepository(s).add_product(
                body.category_id, body.name, body.description, body.image_url)
            await s.commit()
        except Exception as exc:
            await s.rollback()
            raise HTTPException(422, f"не удалось добавить товар: {exc}") from exc
    return {"ok": True, "id": getattr(p, "id", None)}


@router.delete("/merch/products/{product_id}", dependencies=[Depends(_tok)])
async def merch_del_product(product_id: int) -> dict:
    from app.db.repositories import MerchRepository
    async with _session() as s:
        ok = await MerchRepository(s).delete_product(product_id)
        await s.commit()
    if not ok:
        raise HTTPException(404, "товар не найден")
    return {"ok": True}


@router.post("/merch/variants", dependencies=[Depends(_tok)])
async def merch_add_variant(body: MerchVariantBody) -> dict:
    from app.db.repositories import MerchRepository
    async with _session() as s:
        try:
            # add_variant возвращает (variant, created) — раньше id брался
            # из кортежа напрямую и отдавал None
            v, _created = await MerchRepository(s).add_variant(
                body.product_id, body.size, body.color, body.price_rub, body.stock)
            await s.commit()
        except Exception as exc:
            await s.rollback()
            raise HTTPException(422, f"не удалось добавить вариант: {exc}") from exc
    return {"ok": True, "id": getattr(v, "id", None)}


@router.post("/merch/variants/{variant_id}", dependencies=[Depends(_tok)])
async def merch_edit_variant(variant_id: int, body: MerchVariantPatch) -> dict:
    from app.db.models import MerchVariant
    async with _session() as s:
        v = (await s.execute(select(MerchVariant).where(
            MerchVariant.id == variant_id))).scalar_one_or_none()
        if v is None:
            raise HTTPException(404, "вариант не найден")
        if body.price_rub is not None:
            v.price_rub = max(0, int(body.price_rub))
        if body.stock is not None:
            v.stock = max(0, int(body.stock))
        if body.size is not None:
            v.size = body.size
        if body.color is not None:
            v.color = body.color
        await s.commit()
    return {"ok": True}


@router.delete("/merch/variants/{variant_id}", dependencies=[Depends(_tok)])
async def merch_del_variant(variant_id: int) -> dict:
    from app.db.repositories import MerchRepository
    async with _session() as s:
        ok = await MerchRepository(s).delete_variant(variant_id)
        await s.commit()
    if not ok:
        raise HTTPException(404, "вариант не найден")
    return {"ok": True}


@router.get("/merch/reservations", dependencies=[Depends(_tok)])
async def merch_reservations() -> dict:
    from app.db.models import MerchProduct, User
    from app.db.repositories import MerchRepository
    async with _session() as s:
        rows = await MerchRepository(s).all_reserved()
        out = []
        for r in rows:
            owner = None
            if r.reserved_by:
                u = (await s.execute(select(User).where(User.tg_id == r.reserved_by))
                     ).scalar_one_or_none()
                owner = (u.first_name or u.username or str(u.tg_id)) if u else None
            prod = (await s.execute(select(MerchProduct).where(
                MerchProduct.id == r.product_id))).scalar_one_or_none()
            out.append({
                "variantId": r.id, "size": r.size, "color": r.color,
                "reservedBy": r.reserved_by, "reservedByTitle": owner,
                "reservedAt": _dt(r.reserved_at),
                "productName": prod.name if prod else "?",
            })
    return {"items": out}


# ================================ мероприятия ================================

@router.get("/events", dependencies=[Depends(_tok)])
async def events_list() -> dict:
    from app.db.models import Event
    async with _session() as s:
        rows = (await s.execute(select(Event).order_by(Event.date, Event.id))
                ).scalars().all()
    return {"items": [{
        "id": e.id, "title": e.title, "date": e.date, "time": e.time,
        "place": e.place, "meet": e.meet, "description": e.description,
        "imageUrl": e.image_url, "url": e.url, "icon": e.icon,
        "goingCount": len(e.going or []), "createdAt": _dt(e.created_at),
    } for e in rows]}


class EventBody(BaseModel):
    title: str
    date: str = ""
    time: str = ""
    place: str = ""
    meet: str = ""
    description: str = ""
    image_url: str | None = None
    url: str | None = None
    icon: str = "🎪"


@router.post("/events", dependencies=[Depends(_tok)])
async def event_create(body: EventBody) -> dict:
    from app.db.repositories import EventRepository
    if not body.title.strip():
        raise HTTPException(422, "название обязательно")
    async with _session() as s:
        ev = await EventRepository(s).create(**body.model_dump())
        await s.commit()
    return {"ok": True, "id": ev.id}


@router.post("/events/{event_id}", dependencies=[Depends(_tok)])
async def event_update(event_id: int, body: dict) -> dict:
    from app.db.repositories import EventRepository
    allowed = {"title", "date", "time", "place", "meet", "description",
               "image_url", "url", "icon"}
    fields = {k: v for k, v in (body or {}).items() if k in allowed and v is not None}
    if not fields:
        raise HTTPException(422, "нет допустимых полей для изменения")
    async with _session() as s:
        ok = await EventRepository(s).update(event_id, **fields)
        await s.commit()
    if not ok:
        raise HTTPException(404, "мероприятие не найдено")
    return {"ok": True}


@router.delete("/events/{event_id}", dependencies=[Depends(_tok)])
async def event_delete(event_id: int) -> dict:
    from app.db.repositories import EventRepository
    async with _session() as s:
        ok = await EventRepository(s).delete(event_id)
        await s.commit()
    if not ok:
        raise HTTPException(404, "мероприятие не найдено")
    return {"ok": True}


# ================================ подписчики ================================

@router.get("/subscribers", dependencies=[Depends(_tok)])
async def subscribers(limit: int = Query(200, le=2000), offset: int = 0) -> dict:
    from app.db.models import ChannelSubscriber
    async with _session() as s:
        total = (await s.execute(select(func.count(ChannelSubscriber.user_id))
                                 )).scalar() or 0
        rows = (await s.execute(select(ChannelSubscriber).order_by(
            ChannelSubscriber.last_seen_at.desc()).limit(limit).offset(offset))
        ).scalars().all()
    return {"total": total, "items": [{
        "userId": r.user_id, "username": r.username, "firstName": r.first_name,
        "chats": r.chats or [], "everContacted": r.ever_contacted,
        "firstSeen": _dt(r.first_seen), "lastSeen": _dt(r.last_seen_at),
    } for r in rows]}


# ================================ очередь уведомлений ================================

@router.get("/notifications", dependencies=[Depends(_tok)])
async def notifications(limit: int = Query(100, le=500)) -> dict:
    from app.db.models import NotificationQueue
    async with _session() as s:
        rows = (await s.execute(select(NotificationQueue).order_by(
            NotificationQueue.send_at.desc()).limit(limit))).scalars().all()
    return {"items": [{
        "id": n.id, "userId": n.user_id, "kind": n.kind, "text": n.text[:200],
        "sendAt": _dt(n.send_at), "sent": n.sent,
    } for n in rows]}


# ---------- служебные ручки: сиды, рассылка, очистка (до маршрутов с {id}) ----------

class BroadcastBody(BaseModel):
    text: str
    kind: str = "info"
    only_active: bool = True     # только небанутые


@router.post("/broadcast", dependencies=[Depends(_tok)])
async def broadcast(body: BroadcastBody) -> dict:
    """Поставить массовую рассылку в очередь уведомлений (исполняет бот)."""
    from app.db.models import User
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(422, "пустой текст рассылки")
    if len(text) > 3500:
        raise HTTPException(422, "текст слишком длинный (>3500 симв.)")
    stmt = select(User.tg_id)
    if body.only_active:
        stmt = stmt.where(User.is_banned == False)
    async with _session() as s:
        ids = (await s.execute(stmt)).scalars().all()
        from app.services.notifications import queue_notification
        for uid in ids:
            await queue_notification(s, uid, body.kind, text)
        await s.commit()
    running = False
    try:
        from app.console.runtime import runtime
        running = runtime.state == "running"
    except Exception as exc:
        logger.debug("admin API: runtime state lookup failed: %s", exc)
    return {"ok": True, "queued": len(ids), "botRunning": running}


@router.post("/maintenance/seed", dependencies=[Depends(_tok)])
async def maintenance_seed() -> dict:
    """Досеять справочники: достижения, предметы магазина, каталог мерча."""
    out: dict[str, int] = {}
    async with _session() as s:
        from app.handlers.shop import seed_items
        from app.services.achievements import seed_achievements
        out["achievements"] = await seed_achievements(s)
        out["items"] = await seed_items(s)
        try:
            from app.db.repositories import seed_merch_catalog
            out["merchCategories"] = await seed_merch_catalog(s)
        except Exception as exc:
            out["merchError"] = str(exc)[:200]
        await s.commit()
    global _db_init_done
    _db_init_done = True
    return {"ok": True, **out}


@router.post("/notifications/clear", dependencies=[Depends(_tok)])
async def notifications_clear(body: dict | None = None) -> dict:
    """Очистка очереди: только отправленные (по умолчанию) или все."""
    from app.db.models import NotificationQueue
    sent_only = bool((body or {}).get("sent_only", True))
    async with _session() as s:
        stmt = select(NotificationQueue)
        if sent_only:
            stmt = stmt.where(NotificationQueue.sent == True)
        rows = (await s.execute(stmt)).scalars().all()
        for n in rows:
            await s.delete(n)
        await s.commit()
    return {"ok": True, "removed": len(rows)}


@router.delete("/notifications/{notif_id}", dependencies=[Depends(_tok)])
async def notification_delete(notif_id: int) -> dict:
    from app.db.models import NotificationQueue
    async with _session() as s:
        n = (await s.execute(select(NotificationQueue).where(
            NotificationQueue.id == notif_id))).scalar_one_or_none()
        if n is None:
            raise HTTPException(404, "уведомление не найдено")
        await s.delete(n)
        await s.commit()
    return {"ok": True}


# ========================= выдача наград / призов =========================

class GrantItem(BaseModel):
    item_id: int
    quantity: int = 1


class GrantRewards(BaseModel):
    xp: int = 0
    coins: int = 0
    items: list[GrantItem] = []   # предметы → в инвентарь активного питомца
    message: str | None = None    # доп. текст сверху уведомления


@router.post("/users/{tg_id}/grant", dependencies=[Depends(_tok)])
async def grant_rewards(tg_id: int, body: GrantRewards) -> dict:
    """Выдать пользователю XP/монеты/предметы. Пользователь получает в ЛС
    сообщение с ПЕРЕЧИСЛЕНИЕМ всего вручённого (раньше приходил только
    общий текст без состава награды)."""
    from app.db.models import Item, Pet, PetInventory, User
    from app.utils.formatting import apply_xp
    async with _session() as s:
        u = (await s.execute(select(User).where(User.tg_id == tg_id))
             ).scalar_one_or_none()
        if u is None:
            raise HTTPException(404, f"пользователь {tg_id} не найден")
        new_levels: list[int] = []
        parts: list[str] = []
        if body.xp or body.coins:
            u.level, u.xp, new_levels = apply_xp(u.level, u.xp, max(0, body.xp))
            u.coins = max(0, u.coins + body.coins)
        if body.xp:
            parts.append(f"⭐ {body.xp} опыта")
        if body.coins:
            parts.append(f"🪙 {body.coins} монет")

        granted_items: list[dict] = []
        for gi in body.items:
            it = (await s.execute(select(Item).where(Item.id == gi.item_id))
                  ).scalar_one_or_none()
            if it is None:
                raise HTTPException(404, f"предмет {gi.item_id} не найден")
            qty = max(1, int(gi.quantity))
            pet = (await s.execute(
                select(Pet).where(Pet.user_id == tg_id)
                .order_by(Pet.is_archived, Pet.born_at.desc())
            )).scalars().first()
            if pet is None:
                raise HTTPException(400,
                    f"у пользователя нет питомца — предмет «{it.name}» выдать некуда")
            row = (await s.execute(select(PetInventory).where(
                PetInventory.pet_id == pet.id,
                PetInventory.item_id == it.id))).scalar_one_or_none()
            if row is None:
                s.add(PetInventory(pet_id=pet.id, item_id=it.id, quantity=qty))
            else:
                row.quantity += qty
            granted_items.append({"name": it.name, "icon": it.icon, "quantity": qty})
            parts.append(f"{it.icon or '📦'} {it.name} ×{qty}")

        if not parts and not (body.message or "").strip():
            raise HTTPException(400, "нечего выдавать: укажите опыт, монеты, предметы или текст")

        head = (body.message.strip() + "\n\n") if (body.message or "").strip() else ""
        text = head + "🎁 Вам начислено:\n" + "\n".join("• " + p for p in parts) if parts else body.message.strip()
        from app.services.notifications import queue_notification
        queued = await queue_notification(s, tg_id, "reward", text)
        await s.commit()
    return {"ok": True, "level": u.level, "newLevels": new_levels,
            "delivered": parts, "queued": queued, "items": granted_items}


class AchievementGrant(BaseModel):
    code: str


@router.post("/users/{tg_id}/achievements", dependencies=[Depends(_tok)])
async def grant_achievement(tg_id: int, body: AchievementGrant) -> dict:
    """Вручить достижение вручную (с наградами и уведомлением)."""
    from app.db.models import User
    from app.services.achievements import AchievementService
    async with _session() as s:
        u = (await s.execute(select(User).where(User.tg_id == tg_id))
             ).scalar_one_or_none()
        if u is None:
            raise HTTPException(404, f"пользователь {tg_id} не найден")
        svc = AchievementService(s)
        ach = await svc.unlock_by_code(tg_id, body.code.strip())
        if ach is None:
            # возможно, уже открыто — уточним состояние
            from app.db.models import Achievement, UserAchievement
            a = (await s.execute(select(Achievement).where(
                Achievement.code == body.code.strip()))).scalar_one_or_none()
            if a is None:
                await s.rollback()
                raise HTTPException(404, f"достижение «{body.code}» не найдено")
            got = (await s.execute(select(UserAchievement).where(
                UserAchievement.user_id == tg_id,
                UserAchievement.achievement_id == a.id))).scalar_one_or_none()
            await s.commit()
            return {"ok": True, "already": True,
                    "title": a.title, "unlocked": got is not None and got.unlocked_at is not None}
        await s.commit()
    return {"ok": True, "already": False, "title": ach.title}


class AchDelete(BaseModel):
    code: str


@router.post("/users/{tg_id}/achievements/revoke", dependencies=[Depends(_tok)])
async def revoke_achievement(tg_id: int, body: AchDelete) -> dict:
    """Снять (удалить) достижение у пользователя."""
    from app.db.models import Achievement, UserAchievement
    async with _session() as s:
        a = (await s.execute(select(Achievement).where(
            Achievement.code == body.code.strip()))).scalar_one_or_none()
        if a is None:
            raise HTTPException(404, "достижение не найдено")
        row = (await s.execute(select(UserAchievement).where(
            UserAchievement.user_id == tg_id,
            UserAchievement.achievement_id == a.id))).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "у пользователя нет такого достижения")
        await s.delete(row)
        await s.commit()
    return {"ok": True}


# ============================ питомцы: управление ============================

class PetEdit(BaseModel):
    name: str | None = None
    level: int | None = None
    xp: int | None = None
    hunger: float | None = None
    happiness: float | None = None
    energy: float | None = None
    hygiene: float | None = None
    health: float | None = None
    strength: int | None = None
    agility: int | None = None
    intellect: int | None = None
    stage: str | None = None
    sleeping: bool | None = None
    archived: bool | None = None


@router.post("/pets/{pet_id}/edit", dependencies=[Depends(_tok)])
async def pet_edit(pet_id: int, body: PetEdit) -> dict:
    """Изменение питомца из админ-панели.

    Раньше значения молча отбрасывались (NaN из пустых полей формы и
    неверные имена атрибутов), поэтому «изменения не сохранялись».
    Теперь: пропуск незаполненных полей, валидные границы, корректные
    атрибуты модели и подтверждение сохранённых значений в ответе.
    """
    from app.db.models import Pet, PetStage, utcnow
    async with _session() as s:
        p = (await s.execute(select(Pet).where(Pet.id == pet_id))
             ).scalar_one_or_none()
        if p is None:
            raise HTTPException(404, "питомец не найден")
        changed: dict[str, object] = {}
        data = body.model_dump(exclude_none=True)
        for key, val in data.items():
            attr = {"sleeping": "is_sleeping", "archived": "is_archived"}.get(key, key)
            if not hasattr(p, attr):
                continue  # неизвестное поле — игнорируем явно
            if attr == "stage":
                try:
                    v = PetStage(str(val))
                except ValueError as err:
                    raise HTTPException(422, f"неизвестная стадия {val!r}") from err
                p.stage = v
            elif attr == "name":
                name = str(val).strip()[:64]
                if not name:
                    continue
                p.name = name
            elif attr in ("hunger", "happiness", "energy", "hygiene", "health"):
                try:
                    v = float(val)
                except (TypeError, ValueError) as err:
                    raise HTTPException(422, f"поле «{key}» должно быть числом") from err
                setattr(p, attr, max(0.0, min(100.0, v)))
            elif attr in ("level", "strength", "agility", "intellect"):
                try:
                    v = int(float(val))
                except (TypeError, ValueError) as err:
                    raise HTTPException(422, f"поле «{key}» должно быть числом") from err
                setattr(p, attr, max(1, v))
            elif attr == "xp":
                try:
                    v = int(float(val))
                except (TypeError, ValueError) as err:
                    raise HTTPException(422, "поле «опыт» должно быть числом") from err
                p.xp = max(0, v)
            elif isinstance(getattr(p, attr), bool):
                setattr(p, attr, bool(val))
            else:
                setattr(p, attr, val)
            changed[key] = getattr(p, attr).value if attr == "stage" else getattr(p, attr)
        p.last_update = utcnow()
        await s.commit()
    return {"ok": True, "changed": changed}


@router.get("/pets/{pet_id}/inventory", dependencies=[Depends(_tok)])
async def pet_inventory(pet_id: int) -> dict:
    from app.db.models import Item, Pet, PetInventory
    async with _session() as s:
        if (await s.get(Pet, pet_id)) is None:
            raise HTTPException(404, "питомец не найден")
        rows = (await s.execute(
            select(PetInventory, Item).join(Item, Item.id == PetInventory.item_id)
            .where(PetInventory.pet_id == pet_id)
            .order_by(Item.type, Item.name))).all()
    return {"items": [{
        "invId": inv.id, "itemId": inv.item_id, "code": it.code, "name": it.name,
        "icon": it.icon, "type": it.type, "price": it.price,
        "quantity": inv.quantity, "effect": it.effect or {},
    } for inv, it in rows]}


class GiveItemBody(BaseModel):
    item_id: int
    quantity: int = 1


@router.post("/pets/{pet_id}/give_item", dependencies=[Depends(_tok)])
async def pet_give_item(pet_id: int, body: GiveItemBody) -> dict:
    """Выдать предмет питомцу (приз от админа)."""
    from app.db.models import Item, Pet, PetInventory
    qty = max(1, int(body.quantity))
    async with _session() as s:
        if (await s.get(Pet, pet_id)) is None:
            raise HTTPException(404, "питомец не найден")
        it = (await s.execute(select(Item).where(Item.id == body.item_id))
              ).scalar_one_or_none()
        if it is None:
            raise HTTPException(404, "предмет не найден")
        row = (await s.execute(select(PetInventory).where(
            PetInventory.pet_id == pet_id,
            PetInventory.item_id == body.item_id))).scalar_one_or_none()
        if row is None:
            s.add(PetInventory(pet_id=pet_id, item_id=body.item_id, quantity=qty))
        else:
            row.quantity += qty
        # уведомляем владельца с перечислением полученного приза
        owner = (await s.execute(select(Pet.user_id).where(Pet.id == pet_id))
                 ).scalar_one_or_none()
        await s.commit()
    text = f"🎁 Вам выдан приз:\n• {it.icon or '📦'} {it.name} ×{qty}"
    queued = False
    if owner is not None:
        from app.services.notifications import queue_notification
        async with _session() as s2:
            queued = await queue_notification(s2, owner, "reward", text)
            await s2.commit()
    return {"ok": True, "item": it.name, "added": qty,
            "notified": queued, "text": text}


class ItemQtyPatch(BaseModel):
    quantity: int


@router.post("/pets/{pet_id}/inventory/{inv_id}", dependencies=[Depends(_tok)])
async def pet_set_item_qty(pet_id: int, inv_id: int, body: ItemQtyPatch) -> dict:
    from app.db.models import PetInventory
    async with _session() as s:
        row = (await s.execute(select(PetInventory).where(
            PetInventory.id == inv_id, PetInventory.pet_id == pet_id))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "позиция инвентаря не найдена")
        row.quantity = max(0, int(body.quantity))
        if row.quantity == 0:
            await s.delete(row)
        await s.commit()
    return {"ok": True}


@router.delete("/pets/{pet_id}/inventory/{inv_id}", dependencies=[Depends(_tok)])
async def pet_del_item(pet_id: int, inv_id: int) -> dict:
    from app.db.models import PetInventory
    async with _session() as s:
        row = (await s.execute(select(PetInventory).where(
            PetInventory.id == inv_id, PetInventory.pet_id == pet_id))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "позиция инвентаря не найдена")
        await s.delete(row)
        await s.commit()
    return {"ok": True}


# ============================ справочник предметов ============================

@router.get("/items", dependencies=[Depends(_tok)])
async def items_list() -> dict:
    from app.db.models import Item
    async with _session() as s:
        rows = (await s.execute(select(Item).order_by(Item.type, Item.name))
                ).scalars().all()
    return {"items": [{
        "id": i.id, "code": i.code, "name": i.name, "icon": i.icon,
        "type": i.type, "price": i.price, "description": i.description,
        "effect": i.effect or {},
    } for i in rows]}


# ========================== лента активности чатов ==========================

@router.get("/activity", dependencies=[Depends(_tok)])
async def activity_feed(limit: int = Query(80, le=500), chat_id: int = 0) -> dict:
    """Последние засчитанные сообщения + реакции (лента жизни сообщества)."""
    from app.db.models import ChatMessageLog, ReactionLog, User
    async with _session() as s:
        mstmt = (select(ChatMessageLog, User.first_name, User.username)
                 .join(User, User.tg_id == ChatMessageLog.user_id)
                 .order_by(ChatMessageLog.created_at.desc()).limit(limit))
        if chat_id:
            mstmt = mstmt.where(ChatMessageLog.chat_id == chat_id)
        msgs = (await s.execute(mstmt)).all()
        rstmt = (select(ReactionLog, User.first_name, User.username)
                 .join(User, User.tg_id == ReactionLog.to_user)
                 .order_by(ReactionLog.created_at.desc()).limit(limit))
        reacts = (await s.execute(rstmt)).all()
    events = []
    for m, fn, un in msgs:
        events.append({
            "ts": m.created_at, "kind": "message", "chatId": m.chat_id,
            "userId": m.user_id, "userName": fn or un or str(m.user_id),
            "text": ("медиа: " + (m.media_type or "?")) if m.has_media else "",
            "length": m.length, "counted": m.is_counted,
            "skipReason": m.skip_reason,
        })
    for r, fn, un in reacts:
        events.append({
            "ts": r.created_at, "kind": "reaction", "chatId": r.chat_id,
            "userId": r.to_user, "userName": fn or un or str(r.to_user),
            "emoji": r.emoji, "fromUserId": r.from_user, "counted": r.is_counted,
        })
    events.sort(key=lambda e: e["ts"], reverse=True)
    return {"items": [{**e, "ts": _dt(e["ts"])} for e in events[:limit]]}


# ============================== лидерборды ==============================

@router.get("/leaderboards", dependencies=[Depends(_tok)])
async def leaderboards_latest() -> dict:
    from app.db.models import LeaderboardSnapshot
    async with _session() as s:
        rows = (await s.execute(select(LeaderboardSnapshot).order_by(
            LeaderboardSnapshot.created_at.desc()).limit(30))).scalars().all()
    return {"items": [{
        "id": r.id, "period": r.period, "category": r.category,
        "createdAt": _dt(r.created_at), "data": r.data or [],
    } for r in rows]}


# ============================== дуэли питомцев ==============================

@router.get("/duels", dependencies=[Depends(_tok)])
async def duels_week() -> dict:
    from app.db.models import PetDuel
    from app.utils.local_time import now as local_now
    week_key = local_now().strftime("%G-W%V")
    async with _session() as s:
        rows = (await s.execute(
            select(PetDuel).where(PetDuel.week_key == week_key)
            .order_by(PetDuel.score.desc()).limit(100))).scalars().all()
        pet_ids = [d.pet_id for d in rows]
        names: dict[int, tuple[str, str]] = {}
        if pet_ids:
            from app.db.models import Pet
            pets = (await s.execute(select(Pet).where(Pet.id.in_(pet_ids)))
                    ).scalars().all()
            names = {p.id: (p.name, _enum(p.species)) for p in pets}
    return {"weekKey": week_key, "items": [{
        "petId": d.pet_id, "petName": names.get(d.pet_id, ("?", ""))[0],
        "species": names.get(d.pet_id, ("", "?"))[1],
        "wins": d.wins, "losses": d.losses, "fights": d.fights, "score": d.score,
        "updatedAt": _dt(d.updated_at),
    } for d in rows]}


# ============================== настройки чатов ==============================

@router.get("/chats", dependencies=[Depends(_tok)])
async def chats_settings() -> dict:
    """Все чаты, которые вообще попадали в поле зрения бота (по логу сообщений),
    с их настройками кулдауна/минимальной длины. Позволяет админу увидеть и
    настроить любой чат, даже если для него ещё нет явной записи в chat_settings."""
    from app.db.models import ChatMessageLog, ChatSettings
    async with _session() as s:
        settings = {c.chat_id: c for c in (await s.execute(
            select(ChatSettings))).scalars().all()}
        agg = (await s.execute(
            select(ChatMessageLog.chat_id,
                   func.count().label("msgs"),
                   func.max(ChatMessageLog.created_at).label("last_at"))
            .group_by(ChatMessageLog.chat_id)
            .order_by(func.count().desc()).limit(200))).all()
    known = {a.chat_id for a in agg}
    items = [{"chatId": a.chat_id, "messages": a.msgs, "lastActivity": _dt(a.last_at),
              "cooldownSec": (settings[a.chat_id].cooldown_sec if a.chat_id in settings else None),
              "minLength": (settings[a.chat_id].min_length if a.chat_id in settings else None),
              "config": (settings[a.chat_id].config or {} if a.chat_id in settings else None)}
             for a in agg]
    # чаты с явными настройками, но без сообщений — тоже показываем
    for cid, c in settings.items():
        if cid not in known:
            items.append({"chatId": cid, "messages": 0, "lastActivity": None,
                          "cooldownSec": c.cooldown_sec, "minLength": c.min_length,
                          "config": c.config or {}})
    return {"items": items}


class ChatSettingsBody(BaseModel):
    cooldown_sec: int | None = None
    min_length: int | None = None
    config: dict | None = None


@router.post("/chats/{chat_id}", dependencies=[Depends(_tok)])
async def chat_settings_edit(chat_id: int, body: ChatSettingsBody) -> dict:
    from app.db.models import ChatSettings
    async with _session() as s:
        c = await s.get(ChatSettings, chat_id)
        if c is None:
            c = ChatSettings(chat_id=chat_id)
            s.add(c)
        if body.cooldown_sec is not None:
            c.cooldown_sec = max(0, int(body.cooldown_sec))
        if body.min_length is not None:
            c.min_length = max(0, int(body.min_length))
        if body.config is not None:
            c.config = body.config
        await s.commit()
    return {"ok": True}
