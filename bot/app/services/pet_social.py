"""Соцвзаимодействия питомцев: друзья и рекомендации.

Дружба даёт пассивный бонус: +1 счастье в сутки за каждого друга (до 5),
учитывается в еженедельном топе питомцев. Взаимная — оформляется тем же
действием со стороны второго владельца (или «предложением» отсюда).
"""
from __future__ import annotations

import random

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pet, PetFriend


async def list_friends(session: AsyncSession, pet_id: int) -> list[Pet]:
    """Питомцы-друзья (в обе стороны связи)."""
    rows = (await session.execute(
        select(Pet).join(PetFriend, or_(PetFriend.pet_id == Pet.id,
                                        PetFriend.friend_pet_id == Pet.id))
        .where(or_(PetFriend.pet_id == pet_id, PetFriend.friend_pet_id == pet_id))
        .where(Pet.id != pet_id).distinct()
    )).scalars()
    return list(rows)


async def are_friends(session: AsyncSession, a_id: int, b_id: int) -> bool:
    row = (await session.execute(
        select(PetFriend.id).where(
            ((PetFriend.pet_id == a_id) & (PetFriend.friend_pet_id == b_id)) |
            ((PetFriend.pet_id == b_id) & (PetFriend.friend_pet_id == a_id))
        ).limit(1)
    )).scalar_one_or_none()
    return row is not None


MAX_FRIENDS = 5


async def make_friends(session: AsyncSession, pet: Pet, other: Pet) -> tuple[bool, str]:
    """Заводит дружбу. Возвращает (успех, сообщение для экрана)."""
    if pet.id == other.id:
        return False, "Нельзя подружить питомца с самим собой 🙂"
    if await are_friends(session, pet.id, other.id):
        return False, "Они уже лучшие друзья! 💞"
    mine = await list_friends(session, pet.id)
    theirs = await list_friends(session, other.id)
    if len(mine) >= MAX_FRIENDS:
        return False, f"У {pet.name} уже {MAX_FRIENDS} друзей — максимум!"
    if len(theirs) >= MAX_FRIENDS:
        return False, f"У {other.name} уже {MAX_FRIENDS} друзей — максимум!"
    # дубликаты в обе стороны (для простоты чтения списка)
    session.add(PetFriend(pet_id=pet.id, friend_pet_id=other.id))
    session.add(PetFriend(pet_id=other.id, friend_pet_id=pet.id))
    pet.happiness = min(100.0, pet.happiness + 3)
    other.happiness = min(100.0, other.happiness + 3)
    return True, f"💞 {pet.name} и {other.name} теперь друзья!"


async def unfriend(session: AsyncSession, pet_id: int, other_id: int) -> None:
    await session.execute(delete(PetFriend).where(
        or_(
            (PetFriend.pet_id == pet_id) & (PetFriend.friend_pet_id == other_id),
            (PetFriend.pet_id == other_id) & (PetFriend.friend_pet_id == pet_id),
        )
    ))


async def suggest_friend(session: AsyncSession, pet: Pet) -> Pet | None:
    """Рекомендует питомца: уровень +-2, не друг, не сам себя."""
    lo, hi = max(1, pet.level - 2), pet.level + 2
    friends = {f.id for f in await list_friends(session, pet.id)}
    rows = list((await session.execute(
        select(Pet).where(Pet.level >= lo, Pet.level <= hi, Pet.id != pet.id,
                      Pet.is_archived.is_(False))
    )).scalars())
    candidates = [p for p in rows if p.id not in friends]
    if not candidates:
        return None
    return random.choice(candidates[:10])


def render_friend_list(pet: Pet, friends: list[Pet]) -> str:
    lines = [f"🐾 <b>Друзья {pet.name}</b> ({len(friends)}/{MAX_FRIENDS})\n"]
    if not friends:
        lines.append("Пока никого. Жми «Познакомиться» 👇")
    for f in friends:
        lines.append(f"• {f.name} — ур. {f.level}")
    if friends:
        lines.append("\n💡 За каждого друга +1 счастье в сутки.")
    return "\n".join(lines)
