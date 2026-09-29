from __future__ import annotations

import contextlib
from datetime import datetime, timedelta

from sqlalchemy import Integer, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    ChatMessageLog, NotificationSetting, Pet, PetActionLog, PetFriend,
    ReactionLog, User, UserAchievement, UserStat, utcnow,
)
from app.services.access import numeric_chat_id
from app.utils.local_time import now as local_now

class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, tg_id: int) -> User | None:
        return await self.session.get(User, tg_id)

    async def get_with_pet(self, tg_id: int) -> User | None:
        stmt = (
            select(User)
            .options(selectinload(User.pet))
            .where(User.tg_id == tg_id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_or_create(self, tg_id: int, first_name: str = "",
                            username: str | None = None) -> User:
        user = await self.get(tg_id)
        if user is None:
            user = User(tg_id=tg_id, first_name=first_name, username=username)
            self.session.add(user)
            await self.session.flush()
        else:
            if first_name and user.first_name != first_name:
                user.first_name = first_name
            if username is not None and user.username != username:
                user.username = username
        return user

    async def add_xp_coins(self, tg_id: int, xp: int = 0, coins: int = 0) -> None:
        await self.session.execute(
            update(User).where(User.tg_id == tg_id)
            .values(xp=User.xp + xp, coins=User.coins + coins)
        )

    async def top_by(self, column: str, limit: int = 10) -> list[User]:
        col = getattr(User, column)
        stmt = select(User).order_by(col.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def active_since(self, since: datetime) -> list[int]:
        stmt = (
            select(ChatMessageLog.user_id)
            .where(ChatMessageLog.created_at >= since, ChatMessageLog.is_counted.is_(True))
            .distinct()
        )
        return list((await self.session.execute(stmt)).scalars())

    async def bump_stat(self, tg_id: int, key: str, delta: int = 1) -> int:
        row = (await self.session.execute(
            select(UserStat).where(UserStat.user_id == tg_id, UserStat.key == key)
        )).scalar_one_or_none()
        if row is None:
            row = UserStat(user_id=tg_id, key=key, value=max(0, delta))
            self.session.add(row)
        else:
            row.value = max(0, row.value + delta)
        await self.session.flush()
        return row.value

    async def get_stat(self, tg_id: int, key: str) -> int:
        row = (await self.session.execute(
            select(UserStat).where(UserStat.user_id == tg_id, UserStat.key == key)
        )).scalar_one_or_none()
        return row.value if row else 0

    async def set_referrer(self, tg_id: int, referrer_id: int) -> bool:
        user = await self.get(tg_id)
        if user is None or user.tg_id == referrer_id:
            return False
        if user.referrer_id == referrer_id:
            return False
        first_time = user.referrer_id is None
        user.referrer_id = referrer_id
        await self.session.flush()
        return first_time

    async def count_invited(self, referrer_id: int) -> int:
        return (await self.session.execute(
            select(func.count()).select_from(User).where(User.referrer_id == referrer_id)
        )).scalar_one()

    async def notif_settings(self, tg_id: int) -> NotificationSetting:
        ns = await self.session.get(NotificationSetting, tg_id)
        if ns is None:
            ns = NotificationSetting(user_id=tg_id)
            self.session.add(ns)
            await self.session.flush()
        return ns

    async def top_period_messages(self, since: datetime, limit: int = 10) -> list[tuple[User, int]]:
        cnt = func.count().label("cnt")
        sub = (
            select(ChatMessageLog.user_id.label("uid"), cnt)
            .where(ChatMessageLog.created_at >= since, ChatMessageLog.is_counted.is_(True))
            .group_by(ChatMessageLog.user_id)
            .order_by(cnt.desc())
            .limit(limit)
            .subquery()
        )
        stmt = (
            select(User, sub.c.cnt)
            .join(sub, sub.c.uid == User.tg_id)
            .order_by(sub.c.cnt.desc())
        )
        return [(r[0], r[1]) for r in (await self.session.execute(stmt)).all()]

    async def top_period_reactions(self, since: datetime, limit: int = 10) -> list[tuple[User, int]]:
        cnt = func.count().label("cnt")
        sub = (
            select(ReactionLog.to_user.label("uid"), cnt)
            .where(ReactionLog.created_at >= since, ReactionLog.is_counted.is_(True))
            .group_by(ReactionLog.to_user)
            .order_by(cnt.desc())
            .limit(limit)
            .subquery()
        )
        stmt = (
            select(User, sub.c.cnt)
            .join(sub, sub.c.uid == User.tg_id)
            .order_by(sub.c.cnt.desc())
        )
        return [(r[0], r[1]) for r in (await self.session.execute(stmt)).all()]

class ActivityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_message_author(self, chat_id: int, message_id: int) -> int | None:
        stmt = (
            select(ChatMessageLog.user_id)
            .where(ChatMessageLog.chat_id == chat_id,
                   ChatMessageLog.message_id == message_id)
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def log_message(self, entry: ChatMessageLog) -> ChatMessageLog:
        values = dict(
            user_id=entry.user_id, chat_id=entry.chat_id,
            message_id=entry.message_id, length=entry.length,
            has_media=entry.has_media, media_type=entry.media_type,
            is_reply=entry.is_reply, mentions_count=entry.mentions_count,
            is_counted=entry.is_counted, skip_reason=entry.skip_reason,
            created_at=entry.created_at,
        )
        dialect = self.session.bind.dialect.name if self.session.bind else "sqlite"
        try:
            if dialect in ("sqlite", "postgresql"):
                if dialect == "postgresql":
                    from sqlalchemy.dialects.postgresql import insert as ins
                else:
                    from sqlalchemy.dialects.sqlite import insert as ins
                stmt = ins(ChatMessageLog).values(**values).on_conflict_do_nothing(
                    index_elements=["chat_id", "message_id"]
                )
            elif dialect.startswith("mysql"):
                from sqlalchemy.dialects.mysql import insert as ins
                stmt = ins(ChatMessageLog).values(**values).prefix_with("IGNORE")
            else:
                stmt = insert(ChatMessageLog).values(**values)
            await self.session.execute(stmt)
        except IntegrityError:
            await self.session.rollback()
        return entry

    async def messages_count(self, tg_id: int, since: datetime | None = None) -> int:
        stmt = (
            select(func.count())
            .select_from(ChatMessageLog)
            .where(
                ChatMessageLog.user_id == tg_id,
                ChatMessageLog.is_counted.is_(True),
            )
        )
        if since is not None:
            stmt = stmt.where(ChatMessageLog.created_at >= since)
        return (await self.session.execute(stmt)).scalar_one()

    async def daily_counts(self, tg_id: int, days: int = 7,
                           since: datetime | None = None) -> "dict[str, int]":
        from datetime import timezone as _tz
        now = since or datetime.now(_tz.utc)
        start = now - timedelta(days=days - 1)
        day_expr = func.date(ChatMessageLog.created_at)
        stmt = (
            select(day_expr.label("d"), func.count().label("c"))
            .select_from(ChatMessageLog)
            .where(
                ChatMessageLog.user_id == tg_id,
                ChatMessageLog.is_counted.is_(True),
                ChatMessageLog.created_at >= start.replace(hour=0, minute=0, second=0, microsecond=0),
            )
            .group_by("d")
        )
        rows = (await self.session.execute(stmt)).all()
        out: dict[str, int] = {}
        for d, c in rows:
            key = d if isinstance(d, str) else d.isoformat()
            out[key] = int(c)
        return out

    async def media_breakdown(self, tg_id: int,
                              since: datetime | None = None) -> dict[str, int]:
        cond = [ChatMessageLog.user_id == tg_id, ChatMessageLog.is_counted.is_(True)]
        if since is not None:
            cond.append(ChatMessageLog.created_at >= since)
        stmt = (
            select(ChatMessageLog.media_type, func.count(),
                   func.coalesce(func.sum(ChatMessageLog.is_reply.cast(Integer)), 0),
                   func.coalesce(func.sum(ChatMessageLog.mentions_count), 0))
            .where(*cond)
            .group_by(ChatMessageLog.media_type)
        )
        rows = (await self.session.execute(stmt)).all()
        out: dict[str, int] = {}
        replies = 0
        mentions = 0
        known = {"text", "voice", "audio", "video_note", "video", "animation",
                 "sticker", "photo", "document", "poll"}
        for mtype, cnt, rep, men in rows:
            mt = (mtype or "").lower()
            key = mt if mt in known else ("text" if not mt else "other")
            out[key] = out.get(key, 0) + int(cnt)
            replies += int(rep or 0)
            mentions += int(men or 0)
        if replies:
            out["reply"] = replies
        if mentions:
            out["mentions"] = mentions
        return out

    async def bump_counters(self, tg_id: int) -> None:
        await self.session.execute(
            update(User).where(User.tg_id == tg_id)
            .values(messages_count=User.messages_count + 1)
        )

    async def log_reaction(self, entry: ReactionLog) -> bool:
        dup = await self.session.execute(
            select(func.count()).select_from(ReactionLog).where(
                ReactionLog.from_user == entry.from_user,
                ReactionLog.message_id == entry.message_id,
                ReactionLog.emoji == entry.emoji,
            )
        )
        if dup.scalar_one() > 0:
            return False
        self.session.add(entry)
        await self.session.flush()
        return True

    async def day_totals(self, day: datetime) -> list[tuple[int, int]]:
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        stmt = (
            select(ChatMessageLog.user_id, func.count().label("cnt"))
            .where(
                ChatMessageLog.created_at >= start,
                ChatMessageLog.created_at < start + timedelta(days=1),
                ChatMessageLog.is_counted.is_(True),
            )
            .group_by(ChatMessageLog.user_id)
            .order_by(func.count().desc())
            .limit(10)
        )
        return [(r[0], r[1]) for r in (await self.session.execute(stmt)).all()]

class PetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_user(self, user_id: int) -> Pet | None:
        stmt = select(Pet).where(Pet.user_id == user_id,
                                 Pet.is_archived.is_(False))
        return (await self.session.execute(stmt)).scalars().first()

    async def create(self, pet: Pet) -> Pet:
        self.session.add(pet)
        await self.session.flush()
        return pet

    async def all_ids(self) -> list[int]:
        stmt = select(Pet.id)
        return list((await self.session.execute(stmt)).scalars())

    async def count_actions(self, pet_id: int, action: str) -> int:
        stmt = (
            select(func.count())
            .select_from(PetActionLog)
            .where(PetActionLog.pet_id == pet_id, PetActionLog.action == action)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def log_action(self, pet_id: int, action: str, value: int = 0,
                         meta: dict | None = None) -> None:
        self.session.add(PetActionLog(pet_id=pet_id, action=action,
                                      value=value, meta=meta or {}))
        await self.session.flush()

    async def friends(self, pet_id: int) -> list["PetFriend"]:
        stmt = select(PetFriend).where(PetFriend.pet_id == pet_id)
        return list((await self.session.execute(stmt)).scalars())

    async def add_friend(self, pet_id: int, friend_pet_id: int) -> bool:
        dup = (await self.session.execute(
            select(PetFriend).where(PetFriend.pet_id == pet_id,
                                    PetFriend.friend_pet_id == friend_pet_id)
        )).scalar_one_or_none()
        if dup is not None:
            return False
        self.session.add(PetFriend(pet_id=pet_id, friend_pet_id=friend_pet_id))
        self.session.add(PetFriend(pet_id=friend_pet_id, friend_pet_id=pet_id))
        await self.session.flush()
        return True

    async def top_pets(self, limit: int = 10) -> list[Pet]:
        stmt = select(Pet).order_by(Pet.level.desc(), Pet.xp.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(self, user_id: int) -> NotificationSetting:
        ns = await self.session.get(NotificationSetting, user_id)
        if ns is None:
            ns = NotificationSetting(user_id=user_id)
            self.session.add(ns)
            await self.session.flush()
        return ns

class AchievementRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_progress_rows(self, user_id: int) -> list[UserAchievement]:
        from app.db.models import Achievement
        stmt = (
            select(UserAchievement)
            .join(Achievement, Achievement.id == UserAchievement.achievement_id)
            .where(UserAchievement.user_id == user_id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def upsert_progress(self, user_id: int, achievement_id: int,
                              progress: int) -> bool:
        now = local_now()
        row = (await self.session.execute(
            select(UserAchievement).where(
                UserAchievement.user_id == user_id,
                UserAchievement.achievement_id == achievement_id,
            )
        )).scalar_one_or_none()

        if row is None:
            from app.db.models import Achievement
            ach = await self.session.get(Achievement, achievement_id)
            just_unlocked = ach is not None and progress >= ach.condition_value
            row = UserAchievement(user_id=user_id, achievement_id=achievement_id,
                                  progress=progress,
                                  unlocked_at=now if just_unlocked else None)
            self.session.add(row)
            await self.session.flush()
            return just_unlocked

        was_unlocked = row.unlocked_at is not None
        row.progress = max(row.progress, progress)
        if not was_unlocked:
            from app.db.models import Achievement
            ach = await self.session.get(Achievement, achievement_id)
            if ach and row.progress >= ach.condition_value:
                row.unlocked_at = now
                return True
        return False

class SubscriberRepository:

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_membership(self, user_id: int, chat_id: int | None = None,
                                first_name: str = "", username: str | None = None,
                                real_event: bool = False,
                                contacted: bool = False,
                                arrived: bool = True) -> bool:
        from app.db.models import ChannelSubscriber
        uid = int(user_id)
        row = (await self.session.execute(
            select(ChannelSubscriber)
            .where(ChannelSubscriber.user_id == uid)
        )).scalar_one_or_none()
        if row is not None:
            await self.session.refresh(row)
        changed = False
        cid_norm = numeric_chat_id(chat_id) if chat_id is not None else None
        if row is None:
            if not contacted and (cid_norm is None or not arrived):
                return False
            row = ChannelSubscriber(
                user_id=uid, chats=[], first_name=first_name or "",
                username=username)
            self.session.add(row)
            changed = True
        if first_name and row.first_name != first_name:
            row.first_name = first_name
            changed = True
        if username and row.username != username:
            row.username = username
            changed = True
        if cid_norm is not None:
            chats = [numeric_chat_id(c) for c in (row.chats or [])]
            chats = [c for c in chats if c is not None]
            if not arrived:
                if cid_norm in chats:
                    chats.remove(cid_norm)
                    row.chats = chats
                    changed = True
                with contextlib.suppress(Exception):
                    from app.middlewares.gate import reset_subscribe_cache
                    reset_subscribe_cache(uid)
            elif real_event and cid_norm not in chats:
                chats.append(cid_norm)
                row.chats = chats
                changed = True
                with contextlib.suppress(Exception):
                    from app.middlewares.gate import reset_subscribe_cache
                    reset_subscribe_cache(uid)
        row.last_seen_at = utcnow()
        if contacted:
            if not row.ever_contacted:
                row.ever_contacted = True
                changed = True
            row.last_contact_at = utcnow()
            changed = True
        if changed:
            await self.session.commit()
        return changed

    async def add_if_new(self, user_id: int, chat_id: int,
                         first_name: str = "", username: str | None = None) -> bool:
        return await self.record_membership(
            user_id, chat_id, first_name=first_name, username=username,
            real_event=True, arrived=True)

    async def add_membership_sql(self, user_id: int, chat_id: int) -> None:
        await self.record_membership(user_id, chat_id,
                                     real_event=True, arrived=True)

    async def registry_stats(self) -> dict:
        from app.db.models import ChannelSubscriber
        total = int((await self.session.execute(
            select(func.count()).select_from(ChannelSubscriber)
        )).scalar() or 0)
        contacted = int((await self.session.execute(
            select(func.count()).select_from(ChannelSubscriber)
            .where(ChannelSubscriber.ever_contacted.is_(True))
        )).scalar() or 0)
        return {"total": total, "contacted": contacted}

    async def get(self, user_id: int, chat_id: int | None = None):
        from app.db.models import ChannelSubscriber
        row = (await self.session.execute(
            select(ChannelSubscriber)
            .where(ChannelSubscriber.user_id == int(user_id))
        )).scalar_one_or_none()
        if row is None:
            return await self.session.get(ChannelSubscriber, int(user_id))
        await self.session.refresh(row)
        return row

    async def exists(self, user_id: int) -> bool:
        from app.db.models import ChannelSubscriber
        stmt = (select(ChannelSubscriber.user_id)
                .where(ChannelSubscriber.user_id == int(user_id)).limit(1))
        return (await self.session.execute(stmt)).scalars().first() is not None

    async def known_ids(self, user_ids) -> set[int]:
        from app.db.models import ChannelSubscriber
        ids = {int(u) for u in user_ids if u}
        if not ids:
            return set()
        stmt = select(ChannelSubscriber.user_id).where(
            ChannelSubscriber.user_id.in_(ids))
        return {int(r) for r in (await self.session.execute(stmt)).scalars()}

    async def count(self) -> int:
        from app.db.models import ChannelSubscriber
        stmt = select(func.count()).select_from(ChannelSubscriber)
        return int((await self.session.execute(stmt)).scalar_one())

    async def distinct_user_count(self) -> int:
        return await self.count()

    async def member_ids_by_chat(self, chat_id: int) -> set[int]:
        from app.db.models import ChannelSubscriber
        from app.services.access import numeric_chat_id
        cid = numeric_chat_id(chat_id)
        if cid is None:
            return set()
        rows = (await self.session.execute(
            select(ChannelSubscriber.user_id, ChannelSubscriber.chats)
        )).all()
        found: set[int] = set()
        for uid, chats in rows:
            norm = {numeric_chat_id(c) for c in (chats or [])}
            norm.discard(None)
            if cid in norm:
                found.add(int(uid))
        return found

    async def last_seen_user_id(self) -> int | None:
        from app.db.models import ChannelSubscriber
        stmt = select(func.max(ChannelSubscriber.user_id))
        v = (await self.session.execute(stmt)).scalar_one_or_none()
        return int(v) if v is not None else None

class MerchRepository:

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def categories(self) -> list:
        from app.db.models import MerchCategory
        return list((await self.session.execute(
            select(MerchCategory).order_by(MerchCategory.position, MerchCategory.id)
        )).scalars().all())

    async def get_category(self, code: str):
        from app.db.models import MerchCategory
        return (await self.session.execute(
            select(MerchCategory).where(MerchCategory.code == code)
        )).scalar_one_or_none()

    async def add_category(self, code: str, title: str, icon: str = "🧢", position: int = 0):
        from app.db.models import MerchCategory
        cat = MerchCategory(code=code, title=title, icon=icon, position=position)
        self.session.add(cat)
        await self.session.flush()
        return cat

    async def delete_category(self, code: str) -> bool:
        from app.db.models import MerchCategory, MerchProduct, MerchVariant
        cat = await self.get_category(code)
        if cat is None:
            return False
        prod_ids = list((await self.session.execute(
            select(MerchProduct.id).where(MerchProduct.category_id == cat.id)
        )).scalars().all())
        if prod_ids:
            await self.session.execute(
                delete(MerchVariant).where(MerchVariant.product_id.in_(prod_ids)))
            await self.session.execute(
                delete(MerchProduct).where(MerchProduct.category_id == cat.id))
        await self.session.delete(cat)
        return True

    async def products(self, category_id: int) -> list:
        from app.db.models import MerchProduct
        return list((await self.session.execute(
            select(MerchProduct).where(MerchProduct.category_id == category_id)
            .order_by(MerchProduct.id)
        )).scalars().all())

    async def get_product(self, product_id: int):
        from app.db.models import MerchProduct
        return (await self.session.execute(
            select(MerchProduct).where(MerchProduct.id == product_id)
        )).scalar_one_or_none()

    async def add_product(self, category_id: int, name: str, description: str = "",
                          sizes: list | None = None, colors: list | None = None,
                          image_url: str | None = None):
        from app.db.models import MerchProduct
        p = MerchProduct(category_id=category_id, name=name, description=description,
                         sizes=sizes or [], colors=colors or [], image_url=image_url)
        self.session.add(p)
        await self.session.flush()
        return p

    async def delete_product(self, product_id: int) -> bool:
        from app.db.models import MerchProduct, MerchVariant
        p = await self.get_product(product_id)
        if p is None:
            return False
        await self.session.execute(
            delete(MerchVariant).where(MerchVariant.product_id == product_id))
        await self.session.delete(p)
        return True

    async def variants(self, product_id: int) -> list:
        from app.db.models import MerchVariant
        return list((await self.session.execute(
            select(MerchVariant).where(MerchVariant.product_id == product_id)
            .order_by(MerchVariant.size, MerchVariant.color)
        )).scalars().all())

    async def products_with_stock(self, category_id: int) -> list:
        from app.db.models import MerchProduct, MerchVariant
        rows = (await self.session.execute(
            select(MerchProduct.id)
            .join(MerchVariant, MerchVariant.product_id == MerchProduct.id)
            .where(MerchProduct.category_id == category_id, MerchVariant.stock > 0)
            .distinct()
        )).scalars().all()
        ids = set(rows)
        prods = await self.products(category_id)
        return [p for p in prods if p.id in ids]

    async def get_variant(self, variant_id: int):
        from app.db.models import MerchVariant
        return (await self.session.execute(
            select(MerchVariant).where(MerchVariant.id == variant_id)
        )).scalar_one_or_none()

    async def find_variant(self, product_id: int, size: str, color: str):
        from app.db.models import MerchVariant
        return (await self.session.execute(
            select(MerchVariant).where(MerchVariant.product_id == product_id,
                                       MerchVariant.size == size,
                                       MerchVariant.color == color)
        )).scalar_one_or_none()

    async def add_variant(self, product_id: int, size: str, color: str,
                          price_rub: int, stock: int, *, overwrite: bool = True) -> tuple:
        from app.db.models import MerchVariant
        v = await self.find_variant(product_id, size, color)
        if v is not None:
            if overwrite:
                v.price_rub = price_rub
                v.stock = stock
            return v, False
        v = MerchVariant(product_id=product_id, size=size, color=color,
                         price_rub=price_rub, stock=stock)
        self.session.add(v)
        await self.session.flush()
        return v, True

    async def delete_variant(self, variant_id: int) -> bool:
        from app.db.models import MerchVariant
        v = await self.get_variant(variant_id)
        if v is None:
            return False
        await self.session.delete(v)
        return True

    async def reserve(self, variant_id: int, user_id: int) -> str:
        from app.db.models import MerchVariant
        v = await self.get_variant(variant_id)
        if v is None:
            return "not_found"
        if v.stock <= 0:
            return "out_of_stock"
        if v.reserved_by is not None:
            return "already_reserved_self" if v.reserved_by == user_id else "already_reserved"
        res = await self.session.execute(
            update(MerchVariant)
            .where(MerchVariant.id == variant_id,
                   MerchVariant.reserved_by.is_(None),
                   MerchVariant.stock > 0)
            .values(reserved_by=user_id, reserved_at=utcnow()))
        if res.rowcount != 1:
            return "already_reserved"
        return "ok"

    async def confirm_sale(self, variant_id: int) -> dict | None:
        from app.db.models import MerchVariant
        v = await self.get_variant(variant_id)
        if v is None or v.reserved_by is None:
            return None
        buyer = int(v.reserved_by)
        res = await self.session.execute(
            update(MerchVariant)
            .where(MerchVariant.id == variant_id,
                   MerchVariant.reserved_by == buyer,
                   MerchVariant.stock > 0)
            .values(stock=MerchVariant.stock - 1, sold_count=MerchVariant.sold_count + 1,
                    reserved_by=None, reserved_at=None))
        if res.rowcount != 1:
            return None
        return {"variant_id": variant_id, "buyer": buyer}

    async def cancel_reserve(self, variant_id: int) -> dict | None:
        from app.db.models import MerchVariant
        v = await self.get_variant(variant_id)
        if v is None or v.reserved_by is None:
            return None
        buyer = int(v.reserved_by)
        await self.session.execute(
            update(MerchVariant)
            .where(MerchVariant.id == variant_id, MerchVariant.reserved_by == buyer)
            .values(reserved_by=None, reserved_at=None))
        return {"variant_id": variant_id, "buyer": buyer}

    async def all_reserved(self) -> list:
        from app.db.models import MerchVariant
        return list((await self.session.execute(
            select(MerchVariant).where(MerchVariant.reserved_by.is_not(None))
            .order_by(MerchVariant.reserved_at)
        )).scalars().all())

async def seed_merch_catalog(session: AsyncSession) -> int:
    from sqlalchemy import func as _func
    from app.db.models import MerchCategory
    existing = (await session.execute(select(_func.count(MerchCategory.id)))).scalar() or 0
    if existing:
        return 0
    catalog = {
        "hoodie": ("🧥", "Худи", [
            ("Не пиздеть, а делать", 3490),
            ("Боейтесь бляди, крышу рвет", 3490),
            ("При виде меня блядей корежит", 3490),
            ("Молодость, дерзость, хардкор", 3490),
        ]),
        "tshirt": ("👕", "Футболки", [
            ("Не пиздеть, а делать", 1690),
            ("Боейтесь бляди, крышу рвет", 1690),
            ("При виде меня блядей корежит", 1690),
            ("Молодость, дерзость, хардкор", 1690),
        ]),
        "bag": ("👜", "Сумки", [
            ("Не пиздеть, а делать", 1290),
            ("Боейтесь бляди, крышу рвет", 1290),
            ("При виде меня блядей корежит", 1290),
            ("Молодость, дерзость, хардкор", 1290),
        ]),
    }
    sizes = ["S", "M", "L", "XL", "XXL"]
    colors = ["Розовый", "Чёрный", "Белый", "Серый"]
    repo = MerchRepository(session)
    count = 0
    for pos, (_seed_code, (icon, title, products)) in enumerate(catalog.items()):
        code = f"id{pos + 1}"
        cat = await repo.add_category(code, title, icon, pos)
        for pname, price in products:
            p = await repo.add_product(cat.id, pname, f"{title} с фирменным принтом канала.",
                                       sizes=list(sizes), colors=list(colors))
            for s in sizes:
                for c in colors:
                    await repo.add_variant(p.id, s, c, price, 5)
                    count += 1
    return count

class EventRepository:

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def all(self) -> list:
        from app.db.models import Event
        return list((await self.session.execute(
            select(Event).order_by(Event.date, Event.id)
        )).scalars().all())

    async def upcoming(self) -> list:
        today = local_now().date().isoformat()
        return [e for e in await self.all() if (e.date or "") >= today]

    async def get(self, event_id: int):
        from app.db.models import Event
        return (await self.session.execute(
            select(Event).where(Event.id == event_id)
        )).scalar_one_or_none()

    async def create(self, *, title: str, date: str = "", time: str = "",
                     place: str = "", meet: str = "", description: str = "",
                     image_url: str | None = None, url: str | None = None,
                     icon: str = "🎪"):
        from app.db.models import Event
        ev = Event(title=title, date=date, time=time, place=place, meet=meet,
                   description=description, image_url=image_url, url=url,
                   icon=icon, going=[])
        self.session.add(ev)
        await self.session.flush()
        return ev

    async def update(self, event_id: int, **fields) -> bool:
        from app.db.models import Event
        ev = await self.get(event_id)
        if ev is None:
            return False
        for k, v in fields.items():
            if hasattr(ev, k) and v is not None:
                setattr(ev, k, v)
        await self.session.flush()
        return True

    async def delete(self, event_id: int) -> bool:
        from app.db.models import Event
        ev = await self.get(event_id)
        if ev is None:
            return False
        await self.session.delete(ev)
        return True

    async def toggle_going(self, event_id: int, user_id: int) -> tuple[bool, int]:
        ev = await self.get(event_id)
        if ev is None:
            return False, 0
        going = list(ev.going or [])
        if user_id in going:
            going.remove(user_id)
            joined = False
        else:
            going.append(user_id)
            joined = True
        ev.going = going
        await self.session.flush()
        return joined, len(going)
