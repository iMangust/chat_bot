"""PNG-карточка профиля (Этап 5): Pillow, без внешних шрифтов.

Рендерим дефолтным bitmap-шрифтом Pillow с масштабированием — стабильно на
Windows Server без установки шрифтов. Кэшируем путь к последней карточке в
Redis/mem (ключ card:<tg_id>:<hash>) — повторный вызов не перерисовывает.
"""
from __future__ import annotations

import hashlib
import io
import os
import tempfile
from datetime import timezone

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pet, User
from app.services.activity import ActivityService
from app.utils.formatting import progress_bar, xp_needed_for_level

W, H = 900, 460
BG_TOP = (24, 28, 46)
BG_BOTTOM = (38, 44, 74)
ACCENT = (120, 160, 255)
TEXT = (235, 238, 248)
DIM = (150, 158, 180)


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


class ProfileCardRenderer:
    """Чистая функция рисования: User + Pet -> PNG bytes."""

    def render(self, user: User, pet: Pet | None, stats: dict) -> bytes:
        img = Image.new("RGB", (W, H), BG_TOP)
        d = ImageDraw.Draw(img)
        # градиент фона
        for y in range(H):
            t = y / H
            col = tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))
            d.line([(0, y), (W, y)], fill=col)

        f_big = _font(40)
        f_mid = _font(26)
        f_small = _font(20)

        # шапка
        d.rounded_rectangle([24, 24, 144, 144], radius=60, fill=ACCENT)
        initial = (user.first_name or "?")[0].upper()
        d.text((84, 84), initial, fill=(20, 24, 40), font=f_big, anchor="mm")
        d.text((168, 40), user.first_name or "Игрок", fill=TEXT, font=f_big)
        uname = f"@{user.username}" if user.username else ""
        d.text((168, 92), uname, fill=DIM, font=f_mid)

        need = xp_needed_for_level(user.level)
        d.text((24, 176), f"Уровень {user.level}", fill=TEXT, font=f_mid)
        bar_x0, bar_w = 220, 420
        d.rounded_rectangle([bar_x0, 184, bar_x0 + bar_w, 208], radius=12, fill=(55, 62, 92))
        frac = max(0.0, min(1.0, user.xp / max(need, 1)))
        d.rounded_rectangle([bar_x0, 184, bar_x0 + int(bar_w * frac), 208], radius=12, fill=ACCENT)
        d.text((bar_x0 + bar_w + 16, 182), f"{user.xp}/{need} XP", fill=DIM, font=f_small)

        # плитки статов
        tiles = [
            ("🪙 Монеты", str(user.coins)),
            ("🔥 Серия", f"{user.streak_days} дн. (рекорд {user.best_streak})"),
            ("💬 Сообщения", f"{stats.get('total', 0)} всего · {stats.get('week', 0)} за неделю"),
            ("😀 Реакции", f"+{user.reactions_given} / −{user.reactions_received}"),
        ]
        tw = (W - 24 * 2 - 16 * 3) // 4
        for i, (title, value) in enumerate(tiles):
            x0 = 24 + i * (tw + 16)
            d.rounded_rectangle([x0, 236, x0 + tw, 340], radius=14, fill=(48, 55, 84))
            d.text((x0 + 14, 250), title, fill=DIM, font=f_small)
            d.text((x0 + 14, 288), value[:26], fill=TEXT, font=f_small)

        # питомец
        if pet is not None:
            mood_line = (f"{pet.name}: ур. {pet.level} · сытость {int(pet.hunger)}% · "
                         f"счастье {int(pet.happiness)}% · энергия {int(pet.energy)}%")
        else:
            mood_line = "Питомца пока нет — нажми /start"
        d.rounded_rectangle([24, 356, W - 24, 420], radius=14, fill=(48, 55, 84))
        d.text((38, 366), "🐾 Питомец", fill=DIM, font=f_small)
        d.text((38, 392), mood_line[:70], fill=TEXT, font=f_small)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


_renderer = ProfileCardRenderer()


async def render_profile_card(session: AsyncSession, tg_id: int) -> bytes | None:
    from app.db.repositories import UserRepository
    users = UserRepository(session)
    user = await users.get(tg_id)
    if user is None:
        return None
    from app.db.models import Pet as PetModel
    from sqlalchemy import select
    pet = (await session.execute(
        select(PetModel).where(PetModel.user_id == tg_id)
    )).scalar_one_or_none()
    stats = await ActivityService(session).personal_stats(tg_id)
    return _renderer.render(user, pet, stats)


def card_version(png: bytes) -> str:
    return hashlib.sha256(png).hexdigest()[:12]


async def get_or_render_card(session: AsyncSession, tg_id: int) -> tuple[bytes, bool]:
    """Возвращает (png, changed). Кэш версии — Redis/mem (без файловых заморочек)."""
    from app.utils.redis import mem_cached_set
    png = await render_profile_card(session, tg_id)
    if png is None:
        return b"", False
    ver = card_version(png)
    key = f"card:{tg_id}"
    prev = await mem_cached_set(key, ver)
    return png, prev != ver
