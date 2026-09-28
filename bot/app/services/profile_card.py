"""PNG-карточка профиля v2: «питомец в центре», Pillow + системный TTF.

ВАЖНО: растровый шрифт Pillow (`load_default`) покрывает только латиницу —
с ним кириллица и эмодзи превращались в «квадратики» 🟥. Поэтому здесь:
  1) ищем настоящий TTF с кириллицей в системных директориях Windows/Linux
     (+ Bold-вариант для заголовков — карточка перестала быть «серой»);
  2) перед отрисовкой вырезаем символы вне поддерживаемого набором диапазона,
     а эмодзи заменяем ASCII-маркерами ([!], [i]) — никаких □;
  3) если вообще ничего не найдено — встроенный Unicode-шрифт Pillow.

Рендер чистый (данные -> bytes): все источники собираются в card_data.collect().
Кэширование версии — Redis/mem (get_or_render_card).
"""
from __future__ import annotations

import hashlib
import io
import os
import re
from functools import lru_cache

from loguru import logger
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy.ext.asyncio import AsyncSession

W = 900
H = 1320                  # базовая высота холста; реальная считается measure-проходом
MIN_CHART_H = 150         # график активности растягивается на остаток, но не меньше этого
M = 24                    # внешний отступ
PAD = 16                  # внутренний отступ панелей
CARD_BG = (30, 36, 58)    # панель
CARD_BG2 = (40, 47, 72)   # плитка внутри панели
BG_TOP = (18, 21, 36)
BG_BOTTOM = (34, 40, 68)
ACCENT = (120, 160, 255)
GOLD = (240, 195, 90)
TEXT = (235, 238, 248)
DIM = (150, 158, 180)
LINE = (58, 66, 98)

# цвета витальных статов питомца (сытость/счастье/энергия/гигиена/здоровье)
VITAL_COLORS = [(255, 150, 90), (255, 110, 160), (255, 210, 90),
                (110, 200, 255), (110, 230, 150)]

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\tahoma.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
_FONT_BOLD_CANDIDATES = [
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\tahomabd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]

_ALLOWED_RE = re.compile(
    "[^A-Za-z0-9\u0400-\u04FF"          # латиница, цифры, кириллица (+ Ё/ё)
    " .,:;!?\\-–—()/·…%+×«»\"'№&*=<>°]"
)


@lru_cache(maxsize=8)
def _font_path(bold: bool = False) -> str | None:
    for p in (_FONT_BOLD_CANDIDATES if bold else _FONT_CANDIDATES):
        if os.path.exists(p):
            return p
    # фолбэк: есть обычный, но нет жирного — рисуем «жирное» обычным
    if bold:
        return _font_path(False)
    return None


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False):
    path = _font_path(bold)
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
    """Эмодзи -> ASCII-маркеры, прочие неподдерживаемые символы вырезаются."""
    text = (text.replace("⚠", "[!]").replace("❗", "[!]").replace("💡", "[i]")
                .replace("❤", "+").replace("♥", "+"))
    return _ALLOWED_RE.sub("", text).strip()


class ProfileCardRenderer:
    """Чистая функция рисования: пакет данных из card_data.collect() -> PNG bytes."""

    def render(self, data: dict) -> bytes:
        # 1) measure-проход: считаем реальную высоту панелей на «бесконечном»
        #    холсте — так карточка гарантированно вмещает ВСЮ информацию
        #    (раньше фикс. H=840 обрезал панель питомца).
        probe = Image.new("RGB", (W, 5000), BG_TOP)
        self.d = ImageDraw.Draw(probe)
        self._init_fonts()
        y = self._header(data, y=M, draw=False)
        y = self._pet_panel(data, y, draw=False)
        y = self._player_panel(data, y, draw=False)
        needed = y + self._chart_h(data, y) + M   # график ≥120px + нижний отступ

        # 2) реальный рендер на холсте нужной высоты
        img = Image.new("RGB", (W, needed), BG_TOP)
        d = ImageDraw.Draw(img)
        # вертикальный градиент фона
        for yy in range(needed):
            t = yy / needed
            col = tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))
            d.line([(0, yy), (W, yy)], fill=col)

        self._init_fonts()
        self.d = d
        y = self._header(data, y=M)
        y = self._pet_panel(data, y)
        y = self._player_panel(data, y)
        self._activity_chart(data, y)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _init_fonts(self) -> None:
        self.f_big = _font(38, True)
        self.f_mid = _font(24, True)
        self.f_val = _font(26)
        self.f_small = _font(18)
        self.f_tiny = _font(14)

    # ------------------------------------------------------------ helpers
    def put(self, xy, text, **kw):
        self.d.text(xy, clean(text), **kw)

    def fit(self, text: str, font, max_w: int) -> str:
        """Обрезает строку по пиксельную ширину с «…» — текст не вылезает за рамки."""
        d = self.d
        text = clean(text)
        if d.textlength(text, font=font) <= max_w:
            return text
        while text and d.textlength(text + "…", font=font) > max_w:
            text = text[:-1]
        return text.rstrip() + "…"

    # ------------------------------------------------------------------ шапка
    def _header(self, data: dict, y: int, draw: bool = True) -> int:
        d, put, fit = self.d, self.put, self.fit
        user = data["user"]
        ui = data.get("user_info", {})
        rank, total = data.get("rank", (0, 0))

        av = 96
        if not draw:
            return y + av + 14
        cx0, cy0 = M, y
        # аватар: круг с обводкой + первая буква имени
        d.ellipse([cx0, cy0, cx0 + av, cy0 + av], fill=(52, 60, 92), outline=ACCENT, width=3)
        initial = clean((user.first_name or "?")[:1]).upper() or "?"
        put((cx0 + av // 2, cy0 + av // 2), initial, fill=TEXT,
            font=self.f_big, anchor="mm")

        tx = cx0 + av + 18
        put((tx, y + 2), fit(user.first_name or "Игрок", self.f_big, W - tx - 210),
            fill=TEXT, font=self.f_big)
        uname = f"@{user.username}" if user.username else ""
        sub_bits = [b for b in (uname, f"в игре {ui.get('registered', '')}") if b]
        put((tx, y + 50), fit(" · ".join(sub_bits), self.f_small, W - tx - 210),
            fill=DIM, font=self.f_small)

        # XP-бар под именем
        need = max(ui.get("xp_need", 1), 1)
        bx0, bw = tx, W - tx - 210
        frac = max(0.0, min(1.0, user.xp / need))
        put((bx0, y + 72), f"LVL {user.level}", fill=GOLD, font=self.f_small)
        bar_x = bx0 + 78
        bar_w = max(bw - 78, 60)
        d.rounded_rectangle([bar_x, y + 78, bar_x + bar_w, y + 92], radius=7, fill=LINE)
        if frac > 0:
            d.rounded_rectangle([bar_x, y + 78, bar_x + max(14, int(bar_w * frac)), y + 92],
                                radius=7, fill=ACCENT)
        put((bar_x + 8, y + 96), fit(f"{user.xp}/{need} XP до {user.level + 1} ур.",
                                     self.f_tiny, bar_w), fill=DIM, font=self.f_tiny)

        # бейдж места в рейтинге справа
        badge_w = 186
        bx = W - M - badge_w
        d.rounded_rectangle([bx, y + 8, bx + badge_w, y + 84], radius=14, fill=CARD_BG)
        put((bx + badge_w // 2, y + 16), "РАНГ", fill=DIM, font=self.f_tiny, anchor="ma")
        rank_txt = f"{rank}/{total}" if total else "-"
        put((bx + badge_w // 2, y + 34), rank_txt, fill=ACCENT, font=self.f_mid, anchor="ma")
        put((bx + badge_w // 2, y + 62), "по уровням", fill=DIM, font=self.f_tiny, anchor="ma")
        return y + av + 14

    # ------------------------------------------------------- панель питомца
    def _pet_panel(self, data: dict, y: int, draw: bool = True) -> int:
        """Панель питомца. Высота ДИНАМИЧЕСКАЯ: панель подстраивается под
        все данные (характеристики, арена, уход, статусы, подсказки), так что
        информация больше не обрезается."""
        d, put, fit = self.d, self.put, self.fit
        pet = data.get("pet")
        pi = data.get("pet_info")
        x0, x1 = M, W - M
        if pet is None or pi is None:
            h = 120
            if draw:
                d.rounded_rectangle([x0, y, x1, y + h], radius=16, fill=CARD_BG)
                put((x0 + PAD, y + PAD), "Питомец", fill=DIM, font=self.f_mid)
                put((x0 + PAD, y + 52), "Питомца пока нет — нажми /start и заведи друга!",
                    fill=TEXT, font=self.f_val)
            return y + h + 12

        vitals_list = data.get("vitals") or []
        contribs = data.get("stat_contribs") or []
        statuses = pi.get("statuses") or []
        advice = pi.get("advice") or []

        # --- layout (независимо от прохода) --------------------------------
        ax, ay = x0 + PAD, y + PAD                      # круг аватара вида
        bar_y = ay + 92                                 # xp-бар питомца
        vy = bar_y + 34                                 # витальные статы
        vitals_h = len(vitals_list) * 26
        left_bottom = vy + vitals_h                     # низ левой колонки
        rx = x1 - PAD - 250                             # правая колонка
        ry0 = y + PAD + 118
        arena_block = 64
        care_block = 64
        right_bottom = ry0 + 22 + len(contribs) * 24 + 34 \
            + arena_block + 8 + care_block
        h = max(300, int(max(left_bottom, right_bottom) - y)) + 14
        if statuses:
            h += len(statuses) * 24 + 10
        if advice:
            h += 24

        if not draw:
            return y + h + 12

        d.rounded_rectangle([x0, y, x1, y + h], radius=16, fill=CARD_BG)

        # --- левая колонка: аватар вида + имя/стадия/настроение ---
        emoji = pi.get("species_emoji") or ""
        d.ellipse([ax, ay, ax + 84, ay + 84], fill=(52, 60, 92), outline=GOLD, width=2)
        put((ax + 42, ay + 42), clean(emoji) or clean(pet.name[:1]).upper(),
            fill=TEXT, font=self.f_big, anchor="mm")
        tx = ax + 100
        title = f"{pet.name} · {pi['species_title']}"
        put((tx, ay - 2), fit(title, self.f_mid, x1 - tx - PAD), fill=TEXT, font=self.f_mid)
        meta = f"ур. {pet.level} · {pi['stage_title']} · возраст {pi.get('age', '?')}"
        color_acc = pi.get("color_title", "")
        if color_acc and color_acc != "Классический":
            meta += f" · {color_acc}"
        accs = "".join(pi.get("accessories") or [])
        if accs:
            meta += " " + clean(accs)
        put((tx, ay + 30), fit(meta, self.f_small, x1 - tx - PAD), fill=DIM, font=self.f_small)
        mood_line = pi.get("mood_line", "")
        if mood_line:
            put((tx, ay + 54), fit(clean(mood_line), self.f_small, x1 - tx - PAD),
                fill=GOLD, font=self.f_small)

        # xp питомца
        need = max(pi.get("xp_need", 1), 1)
        frac = max(0.0, min(1.0, pet.xp / need))
        d.rounded_rectangle([ax, bar_y, x1 - PAD, bar_y + 10], radius=5, fill=LINE)
        if frac > 0:
            d.rounded_rectangle([ax, bar_y, ax + max(10, int((x1 - PAD - ax) * frac)), bar_y + 10],
                                radius=5, fill=GOLD)
        put((ax, bar_y + 14), fit(f"Опыт питомца {pet.xp}/{need}", self.f_tiny, x1 - ax - PAD),
            fill=DIM, font=self.f_tiny)

        # --- витальные статы (левая колонка, под XP-баром) -----------------
        lab_w = 92
        val_w = 46
        track_x = x0 + PAD + lab_w
        track_w = rx - 14 - val_w - 8 - track_x
        for i, (label, value) in enumerate(vitals_list):
            yy = vy + i * 26
            put((x0 + PAD, yy), label, fill=DIM, font=self.f_small)
            col = VITAL_COLORS[i % len(VITAL_COLORS)]
            v = max(0.0, min(100.0, float(value)))
            d.rounded_rectangle([track_x, yy + 3, track_x + track_w, yy + 13], radius=5, fill=LINE)
            if v > 0:
                wpx = max(10, int(track_w * v / 100.0))
                c = col if v >= 40 else (235, 90, 90)  # критично — красным
                d.rounded_rectangle([track_x, yy + 3, track_x + wpx, yy + 13], radius=5, fill=c)
            put((track_x + track_w + 8, yy), f"{int(v)}%", fill=TEXT, font=self.f_small)

        # --- правая колонка: характеристики, арена, уход -------------------
        ry = ry0
        put((rx, ry), "ХАРАКТЕРИСТИКИ", fill=DIM, font=self.f_tiny)
        ry += 22
        for name, val, hint in contribs:
            put((rx, ry), f"{name}: {val}", fill=TEXT, font=self.f_small)
            put((rx + 118, ry + 2), fit(hint, self.f_tiny, 250 - 118), fill=DIM, font=self.f_tiny)
            ry += 24
        power = pi.get("power", 0)
        put((rx, ry + 2), f"Боевая мощь: {power}", fill=ACCENT, font=self.f_small)

        wins, losses, score = data.get("duel", (0, 0, 0))
        ry += 40
        put((rx, ry), "АРЕНА (НЕДЕЛЯ)", fill=DIM, font=self.f_tiny)
        put((rx, ry + 20), f"{wins} побед · {losses} поражений", fill=TEXT, font=self.f_small)
        put((rx, ry + 42), f"рейтинг {score} оч.", fill=DIM, font=self.f_small)

        care = data.get("care", {})
        ry += 64
        put((rx, ry), "УХОД ЗА ВСЮ ЖИЗНЬ", fill=DIM, font=self.f_tiny)
        feed_n = care.get("feed", 0)
        play_n = (care.get("play", 0) + care.get("rps", 0) + care.get("guess", 0)
                  + care.get("blackjack", 0) + care.get("quiz", 0) + care.get("coin", 0))
        walk_n = care.get("walk", 0)
        wash_n = care.get("wash", 0)
        put((rx, ry + 20), f"покормлений {feed_n} · игр {play_n}", fill=TEXT, font=self.f_small)
        put((rx, ry + 42), f"прогулок {walk_n} · помывок {wash_n}", fill=TEXT, font=self.f_small)

        # --- статусы и подсказки во всю ширину внизу панели ----------------
        sy = y + h - 14
        if advice:
            sy -= 24
            put((x0 + PAD, sy), fit("[i] " + "; ".join(advice), self.f_small, x1 - x0 - PAD * 2),
                fill=(170, 220, 255), font=self.f_small)
        for line in reversed(statuses):
            sy -= 24
            put((x0 + PAD, sy), fit(clean(line), self.f_small, x1 - x0 - PAD * 2),
                fill=GOLD, font=self.f_small)
        return y + h + 12

    # ------------------------------------------------------ панель игрока
    def _player_panel(self, data: dict, y: int, draw: bool = True) -> int:
        d, put, fit = self.d, self.put, self.fit
        user = data["user"]
        stats = data.get("stats", {})
        unlocked, total_ach = data.get("achievements", (0, 0))
        x0, x1 = M, W - M
        h = 176
        if not draw:
            return y + h + 12
        d.rounded_rectangle([x0, y, x1, y + h], radius=16, fill=CARD_BG)
        put((x0 + PAD, y + 10), "ДОСТИЖЕНИЯ И ПРОГРЕСС", fill=DIM, font=self.f_tiny)

        tiles = [
            ("Монеты", f"{user.coins:,}".replace(",", " "), ""),
            ("Стрик", f"{user.streak_days} дн.", f"рекорд {user.best_streak}"),
            ("Сообщения", f"{stats.get('total', 0):,}".replace(",", " "),
             f"+{stats.get('week', 0)} за 7 дн."),
            ("Реакции", f"{user.reactions_given} / {user.reactions_received}",
             "поставил / получил"),
            ("Игры", f"{data.get('games_won', 0)} побед", f"приглашений {data.get('invites', 0)}"),
            ("Ачивки", f"{unlocked}/{total_ach}", "открыто"),
        ]
        cols = 3
        tw = (x1 - x0 - PAD * 2 - 12 * (cols - 1)) // cols
        th = 58
        for i, (title, value, sub) in enumerate(tiles):
            cxx = x0 + PAD + (i % cols) * (tw + 12)
            cyy = y + 34 + (i // cols) * (th + 10)
            d.rounded_rectangle([cxx, cyy, cxx + tw, cyy + th], radius=10, fill=CARD_BG2)
            put((cxx + 10, cyy + 6), fit(title.upper(), self.f_tiny, tw - 20),
                fill=DIM, font=self.f_tiny)
            put((cxx + 10, cyy + 22), fit(value, self.f_val, tw - 20), fill=TEXT, font=self.f_val)
            if sub:
                put((cxx + 10, cyy + 42), fit(sub, self.f_tiny, tw - 20), fill=DIM, font=self.f_tiny)

        # нижняя строка: погода/праздник/средняя длина сообщения
        bits = []
        if data.get("weather"):
            bits.append(clean(data["weather"]))
        if data.get("holiday"):
            bits.append(clean(data["holiday"]))
        avg_len = data.get("avg_len", 0)
        if avg_len:
            bits.append(f"среднее сообщение: {avg_len} зн.")
        if bits:
            put((x0 + PAD, y + h - 26), fit("  ·  ".join(bits), self.f_small, x1 - x0 - PAD * 2),
                fill=DIM, font=self.f_small)
        return y + h + 12

    # ------------------------------------------------ график активности
    @staticmethod
    def _draw_activity_chart(d, daily: dict[str, int], y0: int, f_small) -> None:
        """Совместимость со старым низкоуровневым вызовом: рисует график вручную."""
        from datetime import timedelta
        from app.utils.local_time import now as local_now
        now = local_now()
        days = [(now - timedelta(days=i)).date() for i in range(6, -1, -1)]
        vals = [int((daily or {}).get(dt.isoformat(), 0)) for dt in days]
        maxv = max(vals) if vals else 0
        x0, x1 = M, W - M
        ch = 130
        d.rounded_rectangle([x0, y0, x1, y0 + ch], radius=14, fill=CARD_BG)
        title = "Активность за 7 дней" + (f" · пик {maxv}/день" if maxv else "")
        d.text((x0 + 14, y0 + 8), clean(title), fill=DIM, font=f_small)
        plot_top, plot_bot = y0 + 40, y0 + ch - 26
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

    def _chart_h(self, data: dict, y0: int, draw: bool = True) -> int:
        """Высота графика активности: растягивается до базовой высоты карточки,
        но никогда не меньше MIN_CHART_H (иначе данные панелей полезут в график)."""
        return max(H - y0 - M, MIN_CHART_H)

    def _activity_chart(self, data: dict, y0: int) -> None:
        from datetime import timedelta
        from app.utils.local_time import now as local_now
        d, put = self.d, self.put
        daily = data.get("daily") or {}
        now = local_now()
        days = [(now - timedelta(days=i)).date() for i in range(6, -1, -1)]
        vals = [int(daily.get(dt.isoformat(), 0)) for dt in days]
        maxv = max(vals) if vals else 0
        total_week = sum(vals)
        x0, x1 = M, W - M
        ch = self._chart_h(data, y0)
        d.rounded_rectangle([x0, y0, x1, y0 + ch], radius=16, fill=CARD_BG)
        title = "Активность за 7 дней"
        if total_week:
            title += f" · всего {total_week} сообщ."
        put((x0 + PAD, y0 + 10), title, fill=DIM, font=self.f_tiny)
        if maxv:
            best_day = max(range(len(vals)), key=lambda i: vals[i])
            wd = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][days[best_day].weekday()]
            put((x1 - PAD, y0 + 10), f"пик {maxv} ({wd})", fill=DIM,
                font=self.f_tiny, anchor="ra")

        plot_top, plot_bot = y0 + 44, y0 + ch - 30
        n = len(vals)
        gap = 16
        bw = (x1 - x0 - PAD * 2 - gap * (n - 1)) // n
        base = max(maxv, 1)
        wd_labels = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        for i, v in enumerate(vals):
            bx = x0 + PAD + i * (bw + gap)
            bh = int((plot_bot - plot_top) * (v / base))
            col = GOLD if v == maxv and v else ACCENT
            d.rounded_rectangle([bx, plot_bot - max(bh, 3), bx + bw, plot_bot],
                                radius=4, fill=col if v else LINE)
            lab = str(v) if v else "·"
            tw = d.textlength(clean(lab), font=self.f_small)
            put((bx + bw / 2 - tw / 2, plot_bot - max(bh, 3) - 20), lab,
                fill=TEXT, font=self.f_small)
            dl = wd_labels[days[i].weekday()]
            tw2 = d.textlength(clean(dl), font=self.f_small)
            put((bx + bw / 2 - tw2 / 2, plot_bot + 4), dl, fill=DIM, font=self.f_small)


_renderer = ProfileCardRenderer()


async def render_profile_card(session: AsyncSession, tg_id: int) -> bytes | None:
    from app.services import card_data
    data = await card_data.collect(session, tg_id)
    if data is None:
        return None
    # обогащаем пакет подписями, которые знает только tamagotchi
    pet = data.get("pet")
    if pet is not None and data.get("pet_info"):
        try:
            from app.services.tamagotchi import MOOD_TEXT, SPECIES_DATA, _species_key
            from app.services.card_data import vitals, stat_contribs
            data["pet_info"]["mood_line"] = MOOD_TEXT.get(data["pet_info"]["mood"], "")
            data["pet_info"]["species_emoji"] = SPECIES_DATA.get(
                _species_key(pet), SPECIES_DATA["cat"])["emoji"]
            data["vitals"] = vitals(pet)
            data["stat_contribs"] = stat_contribs(pet)
            # страховка: если collect() не успел обогатить подсказками — считаем здесь
            if "advice" not in data["pet_info"]:
                from app.services.card_data import vital_advice
                data["pet_info"]["advice"] = vital_advice(pet)
        except Exception as exc:  # pragma: no cover
            logger.warning("card: pet labels skipped: {}", exc)
    return _renderer.render(data)


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
