"""PNG-карточка профиля: Pillow + системный TTF-шрифт.

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

W, H = 900, 620
CHART_H = 130   # зона графика активности (под плитками)
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

    def render(self, user: User, pet: Pet | None, stats: dict,
                 daily: dict[str, int] | None = None) -> bytes:
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

        need = xp_needed_for_level(user.level)
        put((24, 176), f"Уровень {user.level}", fill=TEXT, font=f_mid)
        bar_x0, bar_w = 220, 420
        d.rounded_rectangle([bar_x0, 184, bar_x0 + bar_w, 208], radius=12, fill=(55, 62, 92))
        frac = max(0.0, min(1.0, user.xp / max(need, 1)))
        d.rounded_rectangle([bar_x0, 184, bar_x0 + int(bar_w * frac), 208], radius=12, fill=ACCENT)
        put((bar_x0 + bar_w + 16, 182), fit(f"{user.xp}/{need} XP", f_small, W - (bar_x0 + bar_w + 16) - 24),
            fill=DIM, font=f_small)

        # плитки статов: значение переносится на 2 строки внутри плитки,
        # каждая строка урезается по ширине — раньше текст вылезал за рамки.
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

        # график активности за 7 дней (экспорт статистики картинкой)
        self._draw_activity_chart(d, daily or {}, y0=360, f_small=f_small)

        # питомец
        if pet is not None:
            color_tag = ""
            try:
                from app.services.tamagotchi import TamagotchiService
                ckey, worn = TamagotchiService(None).customization(pet)
                if ckey:
                    color_tag = f" · {TamagotchiService(None).PET_COLORS[ckey][0]}"
                if worn:
                    color_tag += " " + "".join(worn)
            except Exception:
                pass
            mood_line = (f"{pet.name}: ур. {pet.level} · сытость {int(pet.hunger)}% · "
                         f"счастье {int(pet.happiness)}% · энергия {int(pet.energy)}%{color_tag}")
        else:
            mood_line = "Питомца пока нет — нажми /start"
        d.rounded_rectangle([24, 516, W - 24, 580], radius=14, fill=(48, 55, 84))
        put((38, 526), "Питомец", fill=DIM, font=f_small)
        put((38, 552), fit(mood_line, f_small, W - 38 * 2), fill=TEXT, font=f_small)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


    @staticmethod
    def _draw_activity_chart(d, daily: dict[str, int], y0: int, f_small) -> None:
        """Столбцы сообщений по дням за последние 7 дней (UTC)."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        days = [(now - timedelta(days=i)).date() for i in range(6, -1, -1)]
        vals = [int(daily.get(dt.isoformat(), 0)) for dt in days]
        maxv = max(vals) if vals else 0
        x0, x1 = 24, W - 24
        d.rounded_rectangle([x0, y0, x1, y0 + CHART_H], radius=14, fill=(48, 55, 84))
        title = "Активность за 7 дней" + (f" · пик {maxv}/день" if maxv else "")
        d.text((x0 + 14, y0 + 8), clean(title), fill=DIM, font=f_small)
        plot_top, plot_bot = y0 + 40, y0 + CHART_H - 26
        n = len(vals)
        gap = 18
        bw = (x1 - x0 - 28 - gap * (n - 1)) // n
        base = max(maxv, 1)
        wd = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        for i, v in enumerate(vals):
            bx = x0 + 14 + i * (bw + gap)
            bh = int((plot_bot - plot_top) * (v / base))
            col = ACCENT if v else (70, 78, 110)
            d.rounded_rectangle([bx, plot_bot - max(bh, 3), bx + bw, plot_bot],
                                radius=4, fill=col)
            lab = str(v) if v else "·"
            tw = d.textlength(clean(lab), font=f_small)
            d.text((bx + bw / 2 - tw / 2, plot_bot - max(bh, 3) - 20), clean(lab),
                   fill=TEXT, font=f_small)
            dl = wd[days[i].weekday()]
            tw2 = d.textlength(clean(dl), font=f_small)
            d.text((bx + bw / 2 - tw2 / 2, plot_bot + 3), clean(dl), fill=DIM, font=f_small)


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
    from app.db.repositories import ActivityRepository
    daily = await ActivityRepository(session).daily_counts(tg_id, days=7)
    return _renderer.render(user, pet, stats, daily=daily)


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
