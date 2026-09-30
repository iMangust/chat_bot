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


def _session():
    # async_sessionmaker сам является async-контекстным менеджером;
    # раньше здесь был "return session_factory()" + "async with await ...",
    # что ломало все ручки API (AttributeError на входе в сессию).
    from app.db.session import session_factory
    return session_factory()


# ============================ контроль доступа ============================
# Панель может слушать 0.0.0.0 (удалённый доступ), поэтому все ручки данных
# закрыты токеном: без него — 401. Токен = WEBHOOK_SECRET_TOKEN из .env, если
# он задан и не дефолтный; иначе — BOT_TOKEN. Фронтенд получает его при
# загрузке страницы (/api/token) и подставляет в заголовок X-Dashboard-Token.

def _api_tokens() -> set[str]:
    """Допустимые токены панели.

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
        for key in ("WEBHOOK_SECRET_TOKEN", "BOT_TOKEN"):
            v = _env_value(key)
            if v:
                env_vals[key] = v
    except Exception:
        pass
    try:
        from app.config import get_settings
        settings = get_settings()
        sec = str(getattr(settings, "webhook_secret_token", "") or "")
        bot = str(getattr(settings, "bot_token", "") or "")
    except Exception:
        sec = bot = ""
    sec = env_vals.get("WEBHOOK_SECRET_TOKEN") or sec
    bot = env_vals.get("BOT_TOKEN") or bot
    if sec and sec != "change-me-in-env":
        toks.add(sec)
    if len(bot) >= 8:
        toks.add(bot)
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
            from app.main import ensure_events_table, _light_migrations
            async with engine.begin() as conn:
                from app.db.models import Base
                await conn.run_sync(Base.metadata.create_all)
                await _light_migrations(conn)
            await ensure_events_table(engine)
            _db_init_done = True
            logger.info("admin API: схема БД проверена/создана при первом запросе")
        except Exception as exc:  # pragma: no cover
            logger.warning("admin API: ленивая инициализация схемы не удалась: {}",
                           exc)


def require_token(token: str | None) -> None:
    if not token or token not in _api_tokens():
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
    from app.db.models import (Achievement, ChannelSubscriber, Event, Pet,
                               User, UserAchievement)
    async with _session() as s:
        users_total = (await s.execute(select(func.count(User.tg_id)))).scalar() or 0
        users_week = (await s.execute(
            select(func.count(User.tg_id)).where(
                User.created_at >= func.now() - timedelta(days=7)))).scalar() or 0
        banned = (await s.execute(
            select(func.count(User.tg_id)).where(User.is_banned == True))).scalar() or 0  # noqa: E712
        pets_total = (await s.execute(select(func.count(Pet.id)).where(
            Pet.is_archived == False))).scalar() or 0  # noqa: E712
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
        stmt = stmt.where(User.is_banned == True)  # noqa: E712
    elif banned == "no":
        stmt = stmt.where(User.is_banned == False)  # noqa: E712
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
    from app.db.models import (NotificationSetting, Pet, ReactionLog,
                               User, UserAchievement, UserStat)
    async with _session() as s:
        u = (await s.execute(select(User).where(User.tg_id == tg_id))
             ).scalar_one_or_none()
        if u is None:
            raise HTTPException(404, f"пользователь {tg_id} не найден")
        pet = (await s.execute(select(Pet).where(Pet.user_id == tg_id)
                               .order_by(Pet.is_archived, Pet.born_at.desc()))
               ).scalars().first()
        achs = (await s.execute(
            select(UserAchievement).where(UserAchievement.user_id == tg_id))
        ).scalars().all()
        stats = (await s.execute(
            select(UserStat).where(UserStat.user_id == tg_id))).scalars().all()
        notif = (await s.execute(select(NotificationSetting).where(
            NotificationSetting.user_id == tg_id))).scalars().first()
        reacts_given = (await s.execute(select(func.count()).select_from(
            ReactionLog).where(ReactionLog.from_user == tg_id))).scalar() or 0
        reacts_recv = (await s.execute(select(func.count()).select_from(
            ReactionLog).where(ReactionLog.to_user == tg_id))).scalar() or 0
        invited = (await s.execute(select(func.count()).select_from(User).where(
            User.referrer_id == tg_id))).scalar() or 0
    return {
        "user": {
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
        },
        "pet": ({
            "id": pet.id, "name": pet.name, "species": _enum(pet.species),
            "stage": _enum(pet.stage), "level": pet.level, "xp": pet.xp,
            "hunger": round(pet.hunger, 1), "happiness": round(pet.happiness, 1),
            "energy": round(pet.energy, 1), "hygiene": round(pet.hygiene, 1),
            "health": round(pet.health, 1), "strength": pet.strength,
            "agility": pet.agility, "intellect": pet.intellect,
            "sleeping": pet.is_sleeping, "archived": pet.is_archived,
            "generation": pet.generation, "bornAt": _dt(pet.born_at),
            "lastUpdate": _dt(pet.last_update),
        } if pet else None),
        "achievements": [{
            "achievementId": a.achievement_id, "code": a.achievement.code,
            "title": a.achievement.title, "icon": a.achievement.icon,
            "rarity": _enum(a.achievement.rarity),
            "progress": a.progress, "unlockedAt": _dt(a.unlocked_at),
        } for a in achs if a.achievement is not None],
        "stats": {st.key: st.value for st in stats},
        "notifications": ({
            "petReminders": notif.pet_reminders,
            "streakReminders": notif.streak_reminders,
            "achievementNotifications": notif.achievement_notifications,
            "dailyReport": notif.daily_report,
        } if notif else None),
        "counters": {"reactionsGiven": reacts_given,
                     "reactionsReceived": reacts_recv, "invited": invited},
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
        await s.commit()
    return {"ok": True, "changed": changed}


# ================================ питомцы ================================

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
            "name": pet.name, "species": _enum(pet.species), "stage": _enum(pet.stage),
            "level": pet.level, "xp": pet.xp, "hunger": round(pet.hunger, 1),
            "happiness": round(pet.happiness, 1), "energy": round(pet.energy, 1),
            "hygiene": round(pet.hygiene, 1), "health": round(pet.health, 1),
            "sleeping": pet.is_sleeping, "archived": pet.is_archived,
            "bornAt": _dt(pet.born_at), "lastUpdate": _dt(pet.last_update),
        })
    return {"total": total, "items": items}


# ================================ достижения ================================

@router.get("/achievements", dependencies=[Depends(_tok)])
async def achievements_list(with_holders: bool = False) -> dict:
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
            counts = {aid: c for aid, c in rows}
    return {"items": [{
        "id": a.id, "code": a.code, "title": a.title, "description": a.description,
        "icon": a.icon, "category": _enum(a.category), "rarity": _enum(a.rarity),
        "conditionType": _enum(a.condition_type),
        "conditionValue": a.condition_value, "rewardXp": a.reward_xp,
        "rewardCoins": a.reward_coins, "hidden": a.is_hidden,
        "holders": counts.get(a.id, 0) if with_holders else None,
    } for a in achs]}


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
    code: str
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
    from app.db.repositories import MerchRepository
    async with _session() as s:
        try:
            c = await MerchRepository(s).add_category(
                body.code, body.title, body.icon, body.position)
            await s.commit()
        except Exception as exc:
            await s.rollback()
            raise HTTPException(422, f"не удалось добавить категорию: {exc}") from exc
    return {"ok": True, "id": getattr(c, "id", None)}


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
    from app.db.models import MerchProduct, MerchVariant, User
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
