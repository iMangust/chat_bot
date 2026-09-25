"""Недельные соревнования питомцев (PVP).

Идея: раз в неделю стартует «арена» — питомцы дерутся на характеристиках.
Победа не рандомная: сила удара = f(level, strength/agility/intellect), но
исход каждой конкретной дуэли слегка разбавлен удачей (±20%), чтобы аутсайдер
мог выиграть одну встречу, но не всю неделю.

Баланс:
  power = level*10 + strength*4 + agility*3 + intellect*2 + mood_bonus
  побеждает тот, у кого power*roll выше; roll ~ U(0.8..1.2)
  награда за победу: +25 очков арены владельцу монетами, питомцу +12 XP;
  поражение: +5 очков (утешительный), питомцу +3 XP.
  Призы недели топ-3 по очам: 300/150/75 монет владельцам.

Антифрод: не больше DAILY_FIGHT_LIMIT боёв на питомца в сутки (кулдаун
FIGHT_COOLDOWN_SEC между боями того же питомца), противник подбирается +-3
уровня — фарм «на слабых» ограничен подбором.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pet, PetDuel, User
from app.i18n import tf
from app.services.notifications import queue_notification
from app.utils.html_text import esc

DUEL_XP_WIN = 12
DUEL_XP_LOSS = 3
SCORE_WIN = 25
SCORE_LOSS = 5
DAILY_FIGHT_LIMIT = 5
FIGHT_COOLDOWN_SEC = 90          # между боями одного питомца (антифрод)
WEEKLY_PRIZES = {1: 300, 2: 150, 3: 75}


def week_key(dt: datetime | None = None) -> str:
    """ISO-ключ текущей недели: '2026-W39' (сброс очков происходит сам собой —
    новая неделя = новая строка PetDuel)."""
    dt = dt or datetime.now(timezone.utc)
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def duel_power(pet: Pet) -> int:
    """«Боевая мощь» питомца из характеристик и уровня.

    Косметика (окрас/аксессуары из settings_extra) в бой НЕ влияет — только
    купленные тренировки и забота о статах.
    """
    mood_bonus = 5 if pet.happiness >= 70 else (0 if pet.happiness >= 40 else -5)
    sick = -10 if pet.health < 50 else 0
    tired = -8 if pet.energy < 25 else 0
    return max(1, pet.level * 10 + pet.strength * 4 + pet.agility * 3
               + pet.intellect * 2 + mood_bonus + sick + tired)


def resolve_duel(a: Pet, b: Pet, rng: random.Random | None = None) -> tuple[Pet, Pet]:
    """Возвращает (winner, loser). Удача ±20% одной встречи не решает неделю."""
    rng = rng or random
    pa = duel_power(a) * rng.uniform(0.8, 1.2)
    pb = duel_power(b) * rng.uniform(0.8, 1.2)
    return (a, b) if pa >= pb else (b, a)


async def get_or_create_row(session: AsyncSession, pet_id: int, wk: str) -> PetDuel:
    row = (await session.execute(
        select(PetDuel).where(PetDuel.pet_id == pet_id, PetDuel.week_key == wk)
    )).scalar_one_or_none()
    if row is None:
        row = PetDuel(pet_id=pet_id, week_key=wk)
        session.add(row)
        await session.flush()
    return row


async def pick_opponent(session: AsyncSession, pet: Pet) -> Pet | None:
    """Соперник: живой (не спит), уровень +-3, не сам питомец.

    Случайность — на стороне Python (random.choice по пулу из 10): кросс-СУБД
    «ORDER BY RAND()/random()» в SQLAlchemy без компиляторов даёт разные имена
    функций в sqlite/Postgres, а выборка равных по уровню всё равно маленькая.
    """
    lo, hi = max(1, pet.level - 3), pet.level + 3
    rows = list((await session.execute(
        select(Pet).where(Pet.level >= lo, Pet.level <= hi,
                          Pet.id != pet.id, Pet.is_sleeping.is_(False),
                          Pet.is_archived.is_(False))
        .limit(10)
    )).scalars())
    return random.choice(rows) if rows else None


def duel_cooldown_left(pet: Pet, now=None) -> int:
    """Секунд до конца кулдауна боя (0 — можно драться)."""
    now = now or datetime.now(timezone.utc)
    last = (pet.settings_extra or {}).get("duel_last_ts")
    if not last:
        return 0
    try:
        ts = datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return 0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0, int(FIGHT_COOLDOWN_SEC - (now - ts).total_seconds()))


async def fight(session: AsyncSession, pet: Pet) -> dict:
    """Проводит один бой. Возвращает результат для экрана или причину отказа."""
    now = datetime.now(timezone.utc)
    wk = week_key(now)
    my = await get_or_create_row(session, pet.id, wk)
    # суточный лимит считаем по счётчику дня в settings_extra (без новой таблицы)
    extra = pet.settings_extra or {}
    today = now.date().isoformat()
    done_today = extra.get("duel_day") == today and extra.get("duel_count", 0) or 0
    if done_today >= DAILY_FIGHT_LIMIT:
        return {"ok": False, "reason": "limit"}
    cd = duel_cooldown_left(pet, now)
    if cd > 0:
        return {"ok": False, "reason": "cooldown", "sec": cd}
    opponent = await pick_opponent(session, pet)
    if opponent is None:
        return {"ok": False, "reason": "no_rivals"}
    winner, loser = resolve_duel(pet, opponent)
    i_won = winner.id == pet.id

    opp_row = await get_or_create_row(session, opponent.id, wk)
    if i_won:
        my.wins += 1; my.score += SCORE_WIN
        opp_row.losses += 1; opp_row.score += SCORE_LOSS
    else:
        my.losses += 1; my.score += SCORE_LOSS
        opp_row.wins += 1; opp_row.score += SCORE_WIN
    my.fights += 1
    opp_row.fights += 1

    from app.services.tamagotchi import TamagotchiService
    svc = TamagotchiService(session)
    await svc.add_pet_xp(winner, DUEL_XP_WIN)
    await svc.add_pet_xp(loser, DUEL_XP_LOSS)

    new_count = done_today + 1
    pet.settings_extra = {**extra, "duel_day": today, "duel_count": new_count,
                          "duel_last_ts": now.isoformat()}
    return {
        "ok": True, "i_won": i_won,
        "opponent": opponent,
        "my_score": my.score, "left": DAILY_FIGHT_LIMIT - new_count,
        "power_a": duel_power(pet), "power_b": duel_power(opponent),
    }


async def arena_screen(session: AsyncSession, tg_id: int) -> tuple[str, object]:
    """Текст арены недели + клавиатура (кнопка «⚔️ Вызов» с учётом кулдауна)."""
    from aiogram.types import InlineKeyboardMarkup
    from app.keyboards.inline import arena_keyboard
    from app.utils.html_text import esc

    wk = week_key()
    rows = await weekly_top(session, wk, 10)
    pet = (await session.execute(
        select(Pet).where(Pet.user_id == tg_id, Pet.is_archived.is_(False))
    )).scalars().first()
    me_pet_id = pet.id if pet else None
    text = arena_text(rows, me_pet_id, wk)
    can_fight = True
    hint = ""
    if pet is not None:
        extra = pet.settings_extra or {}
        today = datetime.now(timezone.utc).date().isoformat()
        left = extra.get("duel_count", 0) if extra.get("duel_day") == today else 0
        if DAILY_FIGHT_LIMIT - left <= 0:
            can_fight = False
            hint = "Лимит боёв на сегодня исчерпан — приходи завтра."
        cd = duel_cooldown_left(pet)
        if cd > 0:
            can_fight = False
            hint = f"Питомец отдыхает после боя — следующий через {cd} сек."
    return text, arena_keyboard(can_fight=can_fight, hint=hint)


async def weekly_top(session: AsyncSession, wk: str | None = None,
                     limit: int = 10) -> list[tuple[Pet, User, PetDuel]]:
    """Топ арены недели: (питомец, владелец, строка боя)."""
    wk = wk or week_key()
    rows = (await session.execute(
        select(PetDuel, Pet, User)
        .join(Pet, Pet.id == PetDuel.pet_id)
        .join(User, User.tg_id == Pet.user_id)
        .where(PetDuel.week_key == wk, PetDuel.fights > 0)
        .order_by(PetDuel.score.desc(), PetDuel.wins.desc())
        .limit(limit)
    )).all()
    return [(p, u, d) for d, p, u in rows]


async def finish_week(session: AsyncSession, prev_week: str | None = None) -> bool:
    """Закрывает прошлую неделю: призы топ-3 + снапшот. Идемпотентно по маркеру."""
    from app.db.models import LeaderboardSnapshot
    now = datetime.now(timezone.utc)
    if prev_week is None:
        # «прошлая» ISO-неделя = неделя, которой принадлежит понедельник минус 1 день
        iso_dt = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) - __import__("datetime").timedelta(days=7)
        prev_week = week_key(iso_dt)
    marker = f"pet_duel_award:{prev_week}"
    already = (await session.execute(
        select(LeaderboardSnapshot.id).where(
            LeaderboardSnapshot.period == "week",
            LeaderboardSnapshot.category == marker).limit(1)
    )).scalar_one_or_none()
    if already is not None:
        return False
    top = await weekly_top(session, prev_week, 10)
    payload = [[p.name, u.first_name, d.score, d.wins] for p, u, d in top]
    session.add(LeaderboardSnapshot(period="week", category="pet_arena", data=payload))
    session.add(LeaderboardSnapshot(period="week", category=marker, data=[]))
    for place, (pet, owner, _row) in enumerate(top[:3], start=1):
        prize = WEEKLY_PRIZES.get(place, 0)
        if prize:
            owner.coins += prize
            await queue_notification(
                session, int(owner.tg_id), "info",
                f"🏟 Питомец {pet.name} занял #{place} в недельной арене! Приз: 🪙 {prize}",
            )
    await session.commit()
    return True


def arena_text(rows: list[tuple[Pet, User, PetDuel]], me_pet_id: int | None,
               wk: str) -> str:
    lines = [f"🏟 <b>Арена питомцев · неделя {wk}</b>\n"]
    if not rows:
        lines.append("Пока никто не дрался — будь первым! Жми «⚔️ Вызов».")
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, (pet, owner, row) in enumerate(rows, start=1):
        me = " 👈 <i>это ты</i>" if me_pet_id and pet.id == me_pet_id else ""
        lines.append(f"{medals.get(i, f'{i}.')} {esc(pet.name)} ({esc(owner.first_name or '')}) — "
                     f"{row.score} оч. · {row.wins}П/{row.losses}П{me}")
    lines.append("\n💡 Бои идут на характеристиках: тренируй 💪🏃🧠 — поднимешься в топе.")
    return "\n".join(lines)
