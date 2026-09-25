"""PNG-карточка профиля (Этап 5): Pillow + системный TTF-шрифт.

ВАЖНО: растровый шрифт Pillow (`load_default`) покрывает только латиницу —
с ним кириллица и эмодзи превращались в «квадратики» 🟥. Поэтому здесь:
  1) ищем настоящий TTF с кириллицой в системных директориях Windows/Linux;
  2) перед отрисовкой вырезаем из строк символы вне поддерживаемого набором
     диапазона (эмодзи в шрифтах ОС нет — вместо «🪙 Монеты» рисуем «Монеты»,
     и т.п.) — никаких □;
  3) если вообще ничего не найдено — используем встроенный Unicode-шрифт
     Pillow (есть кириллица, но нет эмодзи).
Кэшируем версию карточки в Redis/mem — повторный вызов не перерисовывает зря.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pet, User
from app.services.activity import ActivityService
from app.utils.formatting import xp_needed_for_level

W, H = 900, 460
BG_TOP = (24, 28, 46)
BG_BOTTOM = (38, 44, 74)
ACCENT = (120, 160, 255)
TEXT = (235, 238, 248)
DIM = (150, 158, 180)

# кандидаты шрифтов с кириллицей (Windows-first, как прод этого бота)
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\tahoma.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]

# Всё, что НЕ является базовой латиницей/кириллицей/цифрой/обычной пунктуацией,
# вырезается из подписей (эмодзи в TTF-шрифтах отсутствуют → были квадратики).
_ALLOWED_RE = re.compile(
    "[^A-Za-z0-9\u0400-\u04FF"          # латиница, цифры, кириллица (+ Ё/ё)
    " .,:;!?\\-–—()/·…%+×«»\"'№&*=<>_]"
)


@lru_cache(maxsize=8)
def _font_path() -> str | None:
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


@lru_cache(maxsize=32)
def _font(size: int):
    path = _font_path()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1: unicodebitmap
    except TypeError:
        return ImageFont.load_default()


def clean(text: str) -> str:
    """Убирает символы (эмодзи и пр.), которых нет в выбранном шрифте."""
    return _ALLOWED_RE.sub("", text).strip()


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

        def put(xy, text, **kw):
            """draw.text с вырезанием неподдерживаемых шрифтом символов."""
            d.text(xy, clean(text), **kw)

<<<<<<< HEAD
        def fit(text: str, font, max_w: int) -> str:
            """Обрезаёт строку по пиксельную ширину с «…» — текст не вылезает за рамки."""
            text = clean(text)
            if d.textlength(text, font=font) <= max_w:
                return text
            while text and d.textlength(text + "…", font=font) > max_w:
                text = text[:-1]
            return text.rstrip() + "…"

        # шапка (всё через fit — ни одна строка не вылезает за холст/плашки)
        d.rounded_rectangle([24, 24, 144, 144], radius=60, fill=ACCENT)
        initial = clean((user.first_name or "?")[:1]).upper() or "?"
        put((84, 84), initial, fill=(20, 24, 40), font=f_big, anchor="mm")
        put((168, 40), fit(user.first_name or "Игрок", f_big, W - 168 - 24),
            fill=TEXT, font=f_big)
        uname = f"@{user.username}" if user.username else ""
        put((168, 92), fit(uname, f_mid, W - 168 - 24), fill=DIM, font=f_mid)
=======
        # шапка
        d.rounded_rectangle([24, 24, 144, 144], radius=60, fill=ACCENT)
        initial = clean((user.first_name or "?")[:1]).upper() or "?"
        put((84, 84), initial, fill=(20, 24, 40), font=f_big, anchor="mm")
        put((168, 40), user.first_name or "Игрок", fill=TEXT, font=f_big)
        uname = f"@{user.username}" if user.username else ""
        put((168, 92), uname, fill=DIM, font=f_mid)
>>>>>>> origin/main

        need = xp_needed_for_level(user.level)
        put((24, 176), f"Уровень {user.level}", fill=TEXT, font=f_mid)
        bar_x0, bar_w = 220, 420
        d.rounded_rectangle([bar_x0, 184, bar_x0 + bar_w, 208], radius=12, fill=(55, 62, 92))
        frac = max(0.0, min(1.0, user.xp / max(need, 1)))
        d.rounded_rectangle([bar_x0, 184, bar_x0 + int(bar_w * frac), 208], radius=12, fill=ACCENT)
<<<<<<< HEAD
        put((bar_x0 + bar_w + 16, 182), fit(f"{user.xp}/{need} XP", f_small, W - (bar_x0 + bar_w + 16) - 24),
            fill=DIM, font=f_small)

        # плитки статов: значение переносится на 2 строки внутри плитки,
        # каждая строка урезается по ширине — раньше текст вылезал за рамки.
=======
        put((bar_x0 + bar_w + 16, 182), f"{user.xp}/{need} XP", fill=DIM, font=f_small)

        # плитки статов (эмодзи в подписях вырезаются — в TTF их нет)
>>>>>>> origin/main
        tiles = [
            ("Монеты", str(user.coins)),
            ("Серия дней", f"{user.streak_days} дн. (рекорд {user.best_streak})"),
            ("Сообщения", f"{stats.get('total', 0)} всего · {stats.get('week', 0)} за неделю"),
            ("Реакции", f"поставил {user.reactions_given} · получил {user.reactions_received}"),
        ]
        tw = (W - 24 * 2 - 16 * 3) // 4
        inner_w = tw - 28
        for i, (title, value) in enumerate(tiles):
            x0 = 24 + i * (tw + 16)
            d.rounded_rectangle([x0, 236, x0 + tw, 340], radius=14, fill=(48, 55, 84))
<<<<<<< HEAD
            put((x0 + 14, 250), fit(title, f_small, inner_w), fill=DIM, font=f_small)
            v = clean(value)
            words, lines, cur = v.split(" "), [], ""
            for w_ in words:  # greedy word wrap по пиксельной ширине
                trial = (cur + " " + w_).strip()
                if d.textlength(trial, font=f_small) <= inner_w or not cur:
                    cur = trial
                else:
                    lines.append(cur)
                    cur = w_
            if cur:
                lines.append(cur)
            for li, ln in enumerate(lines[:2]):
                put((x0 + 14, 284 + li * 26), fit(ln, f_small, inner_w), fill=TEXT, font=f_small)
            if len(lines) > 2:  # третья строка не влезла — ставим «…» во вторую
                put((x0 + 14, 284 + 26), fit("…", f_small, inner_w), fill=TEXT, font=f_small)
=======
            put((x0 + 14, 250), title, fill=DIM, font=f_small)
            put((x0 + 14, 288), clean(value)[:26], fill=TEXT, font=f_small)
>>>>>>> origin/main

        # питомец
        if pet is not None:
            mood_line = (f"{pet.name}: ур. {pet.level} · сытость {int(pet.hunger)}% · "
                         f"счастье {int(pet.happiness)}% · энергия {int(pet.energy)}%")
        else:
            mood_line = "Питомца пока нет — нажми /start"
        d.rounded_rectangle([24, 356, W - 24, 420], radius=14, fill=(48, 55, 84))
        put((38, 366), "Питомец", fill=DIM, font=f_small)
<<<<<<< HEAD
        put((38, 392), fit(mood_line, f_small, W - 38 * 2), fill=TEXT, font=f_small)
=======
        put((38, 392), clean(mood_line)[:70], fill=TEXT, font=f_small)
>>>>>>> origin/main

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
