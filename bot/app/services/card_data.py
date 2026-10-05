from __future__ import annotations

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Achievement,
    Pet,
    PetActionLog,
    PetDuel,
    User,
    UserAchievement,
)
from app.services.tamagotchi import (
    SPECIES_DATA,
    _aware,
    _species_key,
    compute_mood,
    pet_xp_needed,
)
from app.utils.formatting import xp_needed_for_level
from app.utils.local_time import now as local_now


def _fmt_age(dt) -> str:
    dt = _aware(dt)
    if dt is None:
        return ""
    days = max(0, (local_now() - dt).days)
    if days < 60:
        return f"{days} дн."
    months, d = divmod(days, 30)
    if months < 12:
        return f"{months} мес."
    years, m = divmod(months, 12)
    return f"{years} г {m} мес." if m else f"{years} г"

def vitals(pet: Pet) -> list[tuple[str, float]]:
    return [
        ("Сытость", pet.hunger),
        ("Счастье", pet.happiness),
        ("Энергия", pet.energy),
        ("Гигиена", pet.hygiene),
        ("Здоровье", pet.health),
    ]

def mood_label(mood: str) -> tuple[str, str]:
    """(метка настроения, цвет hex) для карточки."""
    if mood == "great":
        return "Отличное настроение", "#7CE38B"
    if mood == "good":
        return "Хорошее настроение", "#7CE38B"
    if mood == "ok":
        return "Нормально", "#F5D76E"
    if mood == "sleeping":
        return "Спит", "#8AA6FF"
    if mood == "hungry":
        return "Голодный", "#FF9E6B"
    if mood == "sick":
        return "Болеет", "#FF6B6B"
    return "Грустит", "#FFA8C0"

def vital_advice(pet: Pet) -> list[str]:
    tips = []
    if pet.hunger < 35:
        tips.append("проголодался — покорми")
    if pet.hygiene < 35:
        tips.append("грязный — помой")
    if pet.energy < 35 and not pet.is_sleeping:
        tips.append("устал — уложи спать")
    if pet.happiness < 35:
        tips.append("скучает — поиграй")
    if pet.health < 60:
        tips.append("хворает — нужно лечение")
    return tips

def stat_contribs(pet: Pet) -> list[tuple[str, int, str]]:
    return [
        ("Сила", pet.strength, "дуэли: грубая мощь"),
        ("Ловкость", pet.agility, "уклонения в дуэлях"),
        ("Интеллект", pet.intellect, "угадывание в РКН, выдержка в 21"),
    ]

def pet_statuses(pet: Pet) -> list[str]:
    now = local_now()
    out: list[str] = []
    if pet.is_sleeping and pet.sleep_until is not None:
        left = (_aware(pet.sleep_until) - now).total_seconds()
        slept = ""
        if pet.sleep_started_at is not None:
            hs = (now - _aware(pet.sleep_started_at)).total_seconds() / 3600.0
            slept = f", спит {int(hs)} ч" if hs >= 1 else \
                f", спит {max(1, int(hs * 60))} мин"
        if left > 0:
            h, m = int(left // 3600), int(left % 3600 // 60)
            out.append(f"💤 Спит{slept}, проснётся через {h} ч {m} мин")
        else:
            out.append(f"💤 Проспал весь срок{slept} — разбуди его!")
    if pet.walk_until is not None:
        left = (_aware(pet.walk_until) - now).total_seconds()
        if left > 0:
            out.append(f"🌲 На прогулке ({int(left // 60)} мин)")
        else:
            out.append("🌲 Вернулся с прогулки — забери находку!")
    if pet.sick_since is not None:
        out.append(f"🤒 Болеет {_fmt_age(pet.sick_since)}")
    return out

async def collect(session: AsyncSession, tg_id: int) -> dict | None:
    user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
    if user is None:
        return None
    pet = (await session.execute(
        select(Pet).where(Pet.user_id == tg_id, Pet.is_archived == False)
    )).scalar_one_or_none()

    data: dict = {"user": user, "pet": pet}

    try:
        from app.services.activity import ActivityRepository, ActivityService
        stats = await ActivityService(session).personal_stats(tg_id)
        daily = await ActivityRepository(session).daily_counts(tg_id, days=7)
    except Exception as exc:
        logger.warning("card: activity stats skipped: {}", exc)
        stats, daily = {}, {}
    data["stats"] = stats
    data["daily"] = daily

    try:
        unlocked = (await session.execute(
            select(func.count()).select_from(UserAchievement)
            .where(UserAchievement.user_id == tg_id,
                   UserAchievement.unlocked_at.is_not(None))
        )).scalar_one()
        total_ach = (await session.execute(
            select(func.count()).select_from(Achievement)
            .where(Achievement.is_hidden == False)
        )).scalar_one()
    except Exception as exc:
        logger.warning("card: achievements count skipped: {}", exc)
        unlocked = total_ach = 0
    data["achievements"] = (int(unlocked or 0), int(total_ach or 0))

    try:
        ahead = (await session.execute(
            select(func.count(User.tg_id)).where(User.level > user.level)
        )).scalar_one()
        total_players = (await session.execute(
            select(func.count(User.tg_id)).where(User.is_banned == False)
        )).scalar_one()
    except Exception as exc:
        logger.warning("card: rank skipped: {}", exc)
        ahead = total_players = 0
    data["rank"] = (int(ahead or 0) + 1, int(total_players or 0))

    if pet is not None:
        sp = SPECIES_DATA.get(_species_key(pet), SPECIES_DATA["cat"])
        stage_titles = {"egg": "яичко", "baby": "малыш", "teen": "подросток",
                        "adult": "взрослый", "legendary": "легенда"}
        data["pet_info"] = {
            "species_title": sp["title"],
            "species_emoji": sp["emoji"],
            "stage_title": stage_titles.get(getattr(pet.stage, "value", ""), ""),
            "xp_need": pet_xp_needed(pet.level),
            "age": _fmt_age(pet.born_at),
            "mood": compute_mood(pet),
            "statuses": pet_statuses(pet),
            "advice": vital_advice(pet),
            "power": 0,
        }
        try:
            from app.services.pet_duels import duel_power, week_key
            data["pet_info"]["power"] = duel_power(pet)
            row = (await session.execute(
                select(PetDuel).where(PetDuel.pet_id == pet.id,
                                      PetDuel.week_key == week_key())
            )).scalar_one_or_none()
            data["duel"] = (row.wins if row else 0, row.losses if row else 0,
                            row.score if row else 0)
        except Exception as exc:
            logger.warning("card: duel stats skipped: {}", exc)
            data["duel"] = (0, 0, 0)
        try:
            rows = (await session.execute(
                select(PetActionLog.action, func.count())
                .where(PetActionLog.pet_id == pet.id)
                .group_by(PetActionLog.action)
            )).all()
            care = {a: int(c) for a, c in rows}
            feed_n = care.get("feed", 0)
            play_n = (care.get("play", 0) + care.get("rps", 0) + care.get("guess", 0)
                      + care.get("blackjack", 0) + care.get("quiz", 0) + care.get("coin", 0))
            data["care_summary"] = f"Уход: кормлений {feed_n} · игр {play_n}"
        except Exception as exc:
            logger.warning("card: care log skipped: {}", exc)
            data["care_summary"] = ""
    else:
        data["pet_info"] = None
        data["duel"] = (0, 0, 0)
        data["care_summary"] = ""

    try:
        from app.db.repositories import UserRepository
        users = UserRepository(session)
        data["invites"] = await users.get_stat(tg_id, "invites")
        data["games_won"] = await users.get_stat(tg_id, "games_won")
    except Exception:
        data["invites"] = data["games_won"] = 0

    data["user_info"] = {
        "xp_need": xp_needed_for_level(user.level),
        "registered": _fmt_age(user.created_at),
    }
    return data
