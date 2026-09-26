"""Сервис тамагочи: оффлайн-деградация статов, действия, настроение, эволюция.

Баланс (подлежит тюнингу на этапе обкатки):
- деградация в час: hunger -4, happiness -2, energy -2 (днём) / +6 (во сне),
  hygiene -3; при hunger<20 или hygiene<20 здоровье падает на 3/час;
- если health < 50 — питомец болеет (sick_since), лечится предметом «Лекарство»;
- без входа >24 ч дополнительно -15 happiness разово («заскучал»);
- XP питомца: кормление +5, игра +8..15 (зависит от результата мини-игры),
  прогулка +10, тренировка +6. Формула уровня общая: base*level^1.5, base=30.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pet, PetStage, User
from app.i18n import t
from app.utils.formatting import (clamp, holiday_effect_mults, season_for,
                                  stat_bar, weather_info)
from app.utils.html_text import esc

# ключ i18n-каталога: запрет обычного ухода в критическом состоянии
CRIT_MSG = "pet.critical_deny"


def _aware(dt: datetime) -> datetime:
    """datetime из MySQL DATETIME приходит naive — нормализуем к UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# сезонные множители скорости деградации (погода/сезоны)
SEASON_DECAY_MULT = {
    "winter": {"energy": 1.3, "hunger": 1.2},
    "spring": {"happy": 0.8},
    "summer": {"hygiene": 1.2, "hunger": 1.1},
    "autumn": {"happy": 1.15},
}


def season_decay_mult(season: str, stat_key: str) -> float:
    """Множитель деградации стата для сезона (1.0 = без модификации)."""
    return SEASON_DECAY_MULT.get(season, {}).get(stat_key, 1.0)

# скорость деградации статов за 1 час
DECAY_PER_HOUR = {
    "hunger": 4.0,
    "happiness": 2.0,
    "energy_day": 2.0,   # энергия падает днём
    "energy_sleep": 8.0,  # и восстанавливается во сне
    "hygiene": 3.0,
    "health_low_care": 3.0,  # если hunger<20 или hygiene<20
}
PET_XP_BASE = 30.0

STAGE_BY_LEVEL = [
    (15, PetStage.legendary),
    (10, PetStage.adult),
    (6, PetStage.teen),
    (3, PetStage.baby),
    (1, PetStage.egg),
]

# ---------------------------------------------------------------------------
# Виды питомцев: характеристики + предпочтения
#
# decay     — множители скорости падения статов (<1 = медленнее, >1 = быстрее)
# bonus     — механические бонусы (читаются сервисом в действиях)
# prefers   — любимые/нелюбимые занятия: +/- к эффекту действия и доп. XP
# start     — стартовые характеристики (сила/ловкость/интеллект)
# ---------------------------------------------------------------------------
SPECIES_DATA: dict[str, dict] = {
    "cat": {
        "title": "Котёнок",
        "emoji": "🐱",
        "desc": "Самостоятельный весельчак. Обожает игры, не любит воду и ранние подъёмы.",
        "decay": {"hunger": 1.0, "happiness": 1.0, "energy": 1.0, "hygiene": 1.0},
        "bonus": {"play_happy": 1.2, "xp_mult": 1.0, "coin_mult": 1.0, "sleep_bonus": 0.0},
        "prefers": {"play": +6, "wash": -4, "walk": +2, "train": 0, "feed": 0},
        "start": {"strength": 1, "agility": 3, "intellect": 2},
    },
    "dog": {
        "title": "Щенок",
        "emoji": "🐶",
        "desc": "Верный спортсмен. Крепок, вынослив, обожает прогулки и еду. Немедленно откликается.",
        "decay": {"hunger": 0.8, "happiness": 1.0, "energy": 0.9, "hygiene": 1.2},
        "bonus": {"play_happy": 1.0, "xp_mult": 1.0, "coin_mult": 1.0, "sleep_bonus": 0.0},
        "prefers": {"walk": +6, "feed": +3, "play": +2, "train": +2, "wash": -2},
        "start": {"strength": 3, "agility": 2, "intellect": 1},
    },
    "fox": {
        "title": "Лисёнок",
        "emoji": "🦊",
        "desc": "Хитрый кладователь. Приносит больше монет с прогулок, но быстро устаёт и пачкается.",
        "decay": {"hunger": 1.1, "happiness": 1.0, "energy": 1.2, "hygiene": 1.2},
        "bonus": {"play_happy": 1.0, "xp_mult": 1.0, "coin_mult": 1.3, "sleep_bonus": 0.0},
        "prefers": {"walk": +4, "play": +2, "feed": -2, "wash": 0, "train": 0},
        "start": {"strength": 1, "agility": 4, "intellect": 1},
    },
    "owl": {
        "title": "Совёнок",
        "emoji": "🦉",
        "desc": "Ночной интеллектуал. Быстро учится, отлично восстанавливается во сне, но днём вялый.",
        "decay": {"hunger": 1.0, "happiness": 1.0, "energy": 1.3, "hygiene": 0.9},
        "bonus": {"play_happy": 1.0, "xp_mult": 1.3, "coin_mult": 1.0, "sleep_bonus": 4.0},
        "prefers": {"train": +6, "sleep": +3, "play": -2, "feed": 0, "walk": -2},
        "start": {"strength": 1, "agility": 1, "intellect": 4},
    },
    "dragon": {
        "title": "Дракончик",
        "emoji": "🐉",
        "desc": "Редкий универсал (+10% ко всем наградам). Капризен: happiness падает быстрее.",
        "decay": {"hunger": 0.9, "happiness": 1.3, "energy": 1.0, "hygiene": 1.0},
        "bonus": {"play_happy": 1.1, "xp_mult": 1.1, "coin_mult": 1.1, "sleep_bonus": 0.0},
        "prefers": {"train": +2, "feed": +2, "play": +2, "wash": +2, "walk": +2},
        "start": {"strength": 2, "agility": 2, "intellect": 2},
    },
}

# Стартовый питомец выдаётся котёнком; остальных можно «вылупить» за монеты (магазин).
SPECIES_START_PRICE = {"cat": 0, "dog": 150, "fox": 250, "owl": 350, "dragon": 800}

SPECIES_BONUS = {k: v["desc"] for k, v in SPECIES_DATA.items()}


def _species_key(pet) -> str:
    return getattr(pet.species, "value", str(pet.species))


def _species(pet) -> dict:
    return SPECIES_DATA.get(_species_key(pet), SPECIES_DATA["cat"])


def species_pref_delta(pet, action: str) -> int:
    """Насколько питомец любит/не любит действие (к изменению happiness)."""
    return _species(pet)["prefers"].get(action, 0)


MOOD_SPRITES = {
    "great": "😸✨", "good": "😺", "ok": "🐱", "sad": "😿",
    "sick": "🤒😿", "sleeping": "😴🐱", "hungry": "🍽😾",
}


def pet_xp_needed(level: int) -> int:
    return max(int(PET_XP_BASE * (level ** 1.5)), 1)


def compute_stage(level: int) -> PetStage:
    for min_lvl, stage in STAGE_BY_LEVEL:
        if level >= min_lvl:
            return stage
    return PetStage.egg


def compute_mood(pet: Pet) -> str:
    if pet.is_sleeping:
        return "sleeping"
    if pet.health < 50:
        return "sick"
    avg = (pet.hunger + pet.happiness + pet.energy + pet.hygiene) / 4
    if pet.hunger < 25:
        return "hungry"
    if avg >= 80:
        return "great"
    if avg >= 60:
        return "good"
    if avg >= 35:
        return "ok"
    return "sad"


MOOD_TEXT = {
    "great": "Великолепно!", "good": "Хорошее настроение", "ok": "Нормально",
    "sad": "Грустит… удели внимание", "sick": "Больной! Нужно лечение 💊",
    "sleeping": "Спит… не буди 💤", "hungry": "Голодный! Дай поесть 🍎",
}

# i18n-ключ настроения (для переводимых мест); MOOD_TEXT — фолбэк.
MOOD_I18N_KEY = {
    "great": "pet.mood_great", "good": "pet.mood_good", "ok": "pet.mood_ok",
    "sad": "pet.mood_sad", "sick": "pet.mood_sick",
    "sleeping": "pet.mood_sleeping", "hungry": "pet.mood_hungry",
}


def mood_text(mood: str) -> str:
    """Строка настроения питомца (RU-only словарь строк)."""
    key = MOOD_I18N_KEY.get(mood)
    return t(key) if key else MOOD_TEXT.get(mood, "")


class TamagotchiService:
    def __init__(self, session: AsyncSession | None = None) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Оффлайн-деградация
    # ------------------------------------------------------------------
    async def apply_decay(self, pet: Pet, now: datetime | None = None) -> bool:
        """Пересчитывает статы по прошествии времени. Возвращает True, если что-то изменилось.

        Идемпотентно: после применения обновляет pet.last_update.
        """
        now = now or datetime.now(timezone.utc)
        last = _aware(pet.last_update)
        hours = (now - last).total_seconds() / 3600.0
        if hours <= 0:
            return False

        changed = True
        sp = _species(pet)
        d = sp["decay"]
        # сезонная модификация: зимой энергия падает быстрее и т.д.
        try:
            from app.config import get_settings
            season = season_for(now) if get_settings().weather_enabled else ""
        except Exception:
            season = ""
        decay_hunger = DECAY_PER_HOUR["hunger"] * d.get("hunger", 1.0) * season_decay_mult(season, "hunger")
        decay_happy = DECAY_PER_HOUR["happiness"] * d.get("happiness", 1.0) * season_decay_mult(season, "happy")
        decay_energy_day = DECAY_PER_HOUR["energy_day"] * d.get("energy", 1.0) * season_decay_mult(season, "energy")
        decay_hygiene = DECAY_PER_HOUR["hygiene"] * d.get("hygiene", 1.0) * season_decay_mult(season, "hygiene")

        if pet.is_sleeping:
            if pet.sleep_until and now >= _aware(pet.sleep_until):
                pet.is_sleeping = False
                pet.sleep_until = None
                pet.energy = clamp(100 + sp["bonus"]["sleep_bonus"])  # сова спит «лучше всех»
                pet.happiness = clamp(pet.happiness + species_pref_delta(pet, "sleep"))
            else:
                pet.energy = clamp(pet.energy
                                   + (DECAY_PER_HOUR["energy_sleep"] + sp["bonus"]["sleep_bonus"]) * hours)
                pet.hunger = clamp(pet.hunger - decay_hunger * 0.5 * hours)
        else:
            pet.energy = clamp(pet.energy - decay_energy_day * hours)
            pet.hunger = clamp(pet.hunger - decay_hunger * hours)

        pet.happiness = clamp(pet.happiness - decay_happy * hours)
        pet.hygiene = clamp(pet.hygiene - decay_hygiene * hours)

        # «заскучал» — однократно, если не заходили дольше суток
        if hours >= 24 and "bored_penalty" not in (pet.settings_extra or {}):
            pet.happiness = clamp(pet.happiness - 15)
            pet.settings_extra = {**(pet.settings_extra or {}), "bored_penalty": now.isoformat()}

        # здоровье падает при плохом уходе
        if pet.hunger < 20 or pet.hygiene < 20:
            pet.health = clamp(pet.health - DECAY_PER_HOUR["health_low_care"] * hours)
            if pet.health < 50 and pet.sick_since is None:
                pet.sick_since = now
        elif pet.health < 100 and pet.sick_since is None:
            # медленное восстановление, если уход хороший
            pet.health = clamp(pet.health + 1.0 * hours)

        # прогулка завершилась? НЕ снимаем walk_until здесь — иначе потеряется
        # событие и награда (кто первый вызовет apply_decay, тот «съест» флаг).
        # Снимает хендлер после того, как заберёт результат через finish_walk_event.
        walk_finished = bool(pet.walk_until) and now >= _aware(pet.walk_until)

        pet.last_update = now
        return changed or walk_finished

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------
    def _check_cooldown(self, pet: Pet, action: str, seconds: int,
                        now: datetime) -> tuple[bool, int]:
        """Кулдаун действия хранится в pet.settings_extra как '<action>_at'."""
        key = f"{action}_at"
        ts = (pet.settings_extra or {}).get(key)
        if ts:
            elapsed = (now - datetime.fromisoformat(ts)).total_seconds()
            if elapsed < seconds:
                return False, int(seconds - elapsed)
        return True, 0

    def _set_cooldown(self, pet: Pet, action: str, now: datetime) -> None:
        pet.settings_extra = {**(pet.settings_extra or {}), f"{action}_at": now.isoformat()}

    async def feed(self, pet: Pet, effect: dict[str, float]) -> str:
        """Эффект из item.effect, напр. {"hunger": +25, "happiness": +5}. Кулдаун 60 сек."""
        now = datetime.now(timezone.utc)
        await self.apply_decay(pet, now)
        if self.is_critical(pet):
            return t(CRIT_MSG)
        ok, wait = self._check_cooldown(pet, "feed", 60, now)
        if not ok:
            return t("pet.cooldown_feed", sec=wait)
        self._set_cooldown(pet, "feed", now)
        # праздничный модификатор: сытость от еды ×N (Канун НГ и т.п.)
        hol = holiday_effect_mults(now)
        hunger_mult = hol.get("feed_hunger", 1.0)
        # «вкусность» еды = сумма положительных эффектов; любимая еда даёт доп. счастье
        tastiness = sum(v for k, v in effect.items() if k == "hunger" and v > 0) * hunger_mult
        pref = species_pref_delta(pet, "feed")
        bonus_happy = max(0, pref) + (3 if tastiness >= 40 else 0)
        for stat, delta in effect.items():
            if hasattr(pet, stat):
                if stat == "hunger":
                    delta *= hunger_mult
                setattr(pet, stat, clamp(getattr(pet, stat) + delta))
        if bonus_happy:
            pet.happiness = clamp(pet.happiness + bonus_happy)
        xp = int(5 * _species(pet)["bonus"]["xp_mult"] * hol.get("xp", 1.0))
        await self.add_pet_xp(pet, xp)
        tail = " Очень вкусно!" if pref > 0 else (" ...не восторг, но съел." if pref < 0 else "!")
        return t("pet.eaten", tail=tail)

    async def play(self, pet: Pet, won: bool) -> str:
        """Мини-игра завершена; won — результат. Кулдаун 120 сек. Тратит энергию."""
        now = datetime.now(timezone.utc)
        await self.apply_decay(pet, now)
        if self.is_critical(pet):
            return t(CRIT_MSG)
        if pet.is_sleeping:
            return t("pet.sleeping_deny")
        if pet.energy < 15:
            return t("pet.too_tired_play")
        ok, wait = self._check_cooldown(pet, "game", 120, now)
        if not ok:
            return f"⏳ Питомец запыхался! Подожди {wait} сек."
        self._set_cooldown(pet, "game", now)

        sp = _species(pet)
        mult = sp["bonus"]["play_happy"]
        # День св. Валентина: игры приносят +50% счастья
        mult *= holiday_effect_mults(now).get("play_happy", 1.0)
        pref = species_pref_delta(pet, "play")
        pet.energy = clamp(pet.energy - 10)
        pet.hygiene = clamp(pet.hygiene - 5)
        xp_base = 15 if won else 8
        xp = int(xp_base * sp["bonus"]["xp_mult"] * holiday_effect_mults(now).get("xp", 1.0))
        if won:
            pet.happiness = clamp(pet.happiness + 12 * mult + pref)
            await self.add_pet_xp(pet, xp)
            return t("pet.won_game", xp=xp)
        pet.happiness = clamp(pet.happiness + 5 * mult + pref)
        await self.add_pet_xp(pet, xp)
        return t("pet.lost_game", xp=xp)

    # ------------------------------------------------------------------
    # Мини-игры: честная игра с характеристиками питомца
    # ------------------------------------------------------------------
    @staticmethod
    def rps_beats(hand: str) -> str:
        """Ход, который побеждает указанный."""
        return {"rock": "paper", "paper": "scissors", "scissors": "rock"}[hand]

    def guess_range(self, pet: Pet) -> tuple[int, int]:
        """Диапазон «угадай число»: интеллект расширяет подсказки (сужает диапазон)."""
        half = max(3, 10 - pet.intellect // 2)   # L-интеллект 1 → ±10, 15+ → ±3
        secret = random.randint(1, 20)
        lo, hi = max(1, secret - half), min(20, secret + half)
        return secret, (lo, hi)

    def reaction_ms_budget(self, pet: Pet) -> int:
        """Бюджет реакции в мс: ловкость даёт доп. время (база 1500 + 60*agility)."""
        return 1500 + pet.agility * 60

    async def sleep(self, pet: Pet, hours: int = 8) -> str:
        now = datetime.now(timezone.utc)
        await self.apply_decay(pet, now)
        if self.is_critical(pet):
            return t(CRIT_MSG)
        if pet.is_sleeping:
            return t("pet.already_sleeping")
        pet.is_sleeping = True
        pet.sleep_until = now + timedelta(hours=hours)
        self._set_cooldown(pet, "sleep", now)
        return t("pet.fell_asleep", time=f"{pet.sleep_until:%H:%M}")

    async def wash(self, pet: Pet) -> str:
        now = datetime.now(timezone.utc)
        await self.apply_decay(pet, now)
        if self.is_critical(pet):
            return t(CRIT_MSG)
        ok, wait = self._check_cooldown(pet, "wash", 300, now)
        if not ok:
            return f"⏳ Мыться можно раз в 5 минут (осталось {wait} сек)."
        self._set_cooldown(pet, "wash", now)
        pet.hygiene = clamp(pet.hygiene + 40)
        # нелюбимое занятие: кошки и собаки по-разному реагируют на воду
        pet.happiness = clamp(pet.happiness - 3 + species_pref_delta(pet, "wash"))
        xp = int(4 * _species(pet)["bonus"]["xp_mult"])
        await self.add_pet_xp(pet, xp)
        return t("pet.washed")

    async def heal(self, pet: Pet) -> str:
        now = datetime.now(timezone.utc)
        await self.apply_decay(pet, now)
        if pet.sick_since is None and pet.health >= 70:
            return t("pet.not_sick")
        pet.health = clamp(pet.health + 35)
        if pet.health >= 60:
            pet.sick_since = None
        await self.add_pet_xp(pet, 5)
        return t("pet.healed")

    async def train(self, pet: Pet, stat: str) -> str:
        """Тренировка strength/agility/intellect. Кулдаун 180 сек, тратит энергию."""
        now = datetime.now(timezone.utc)
        await self.apply_decay(pet, now)
        if self.is_critical(pet):
            return t(CRIT_MSG)
        if stat not in ("strength", "agility", "intellect"):
            return "❓ Неизвестная тренировка."
        if pet.energy < 20:
            return "😩 Мало энергии для тренировки."
        ok, wait = self._check_cooldown(pet, "train", 180, now)
        if not ok:
            return f"⏳ Перерыв между тренировками: {wait} сек."
        self._set_cooldown(pet, "train", now)
        pet.energy = clamp(pet.energy - 15)
        pet.hunger = clamp(pet.hunger - 8)
        # характеристики растут быстрее, если это «профильная» тренировка вида
        stat_pref = {"strength": "dog", "agility": "fox", "intellect": "owl"}.get(stat)
        gain = 1 + (pet.level // 5)
        if stat_pref and _species_key(pet) == stat_pref:
            gain += 1  # профильная тренировка даёт +1 к приросту
        setattr(pet, stat, getattr(pet, stat) + gain)
        sp = _species(pet)
        xp = int(6 * sp["bonus"]["xp_mult"] * (1.2 if species_pref_delta(pet, "train") > 0 else 1.0))
        await self.add_pet_xp(pet, xp)
        label = {"strength": t("pet.train_stat"), "agility": t("pet.train_agi"),
                 "intellect": t("pet.train_int")}[stat]
        return t("pet.train_done", label=label, gain=gain)

    async def start_walk(self, pet: Pet, hours: int = 2) -> str:
        now = datetime.now(timezone.utc)
        await self.apply_decay(pet, now)
        if self.is_critical(pet):
            return t(CRIT_MSG)
        if pet.walk_until:
            left = int((_aware(pet.walk_until) - now).total_seconds() // 60)
            return t("pet.walk_already", minutes=left)
        if pet.is_sleeping:
            return "😴 Сначала разбуди питомца."
        pet.walk_until = now + timedelta(hours=hours)
        return t("pet.walk_started", hours=hours)

    def finish_walk_event(self, pet: Pet) -> tuple[str, int, int]:
        """Случайное событие прогулки. Возвращает (текст, delta_coins, delta_xp).

        Дружба питомцев: «познакомился» с шансом ~10% — бот позже
        подберёт случайного питомца-друга (флаг в settings_extra['pending_friend']).
        """
        roll = random.random()
        sp = _species(pet)
        # Хэллоуин и пр.: прогулки находят ×N монет; xp по празднику тоже множится
        hol = holiday_effect_mults(datetime.now(timezone.utc))
        coin_mult = sp["bonus"]["coin_mult"] * hol.get("walk_coins", 1.0)
        xp_mult = sp["bonus"]["xp_mult"]
        pref_bonus = species_pref_delta(pet, "walk")  # собаки обожают гулять
        if roll < 0.35:
            c = int(random.randint(5, 15) * coin_mult)
            return f"🪙 Нашёл монетки на прогулке! +{c} монет", c, int(10 * xp_mult)
        if roll < 0.45:
            pet.settings_extra = {**(pet.settings_extra or {}), "pending_friend": True}
            pet.happiness = clamp(pet.happiness + 10 + pref_bonus)
            return ("🐾 Познакомился с другим питомцем! Счастье +" + str(10 + pref_bonus)
                    + "\n   (возможно, станет другом — загляни в 🐾 Друзья)"), 0, int(12 * xp_mult)
        if roll < 0.55:
            pet.happiness = clamp(pet.happiness + 10 + pref_bonus)
            return "🐾 Познакомился с другим питомцем! Счастье +" + str(10 + pref_bonus), 0, int(12 * xp_mult)
        if roll < 0.70:
            pet.hygiene = clamp(pet.hygiene - 15)
            return "💦 Упал в лужу… Гигиена −15", 0, int(8 * xp_mult)
        if roll < 0.80:
            pet.health = clamp(pet.health - 10)
            return "🤧 Простудился на ветру. Здоровье −10", 0, int(8 * xp_mult)
        pet.happiness = clamp(pet.happiness + 5 + pref_bonus)
        return "🌳 Просто хорошо погулял. Счастье +" + str(5 + pref_bonus), 0, int(10 * xp_mult)

    # ------------------------------------------------------------------
    # XP и эволюция
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Кастомизация: окрасы и аксессуары — косметика за монеты.
    # Хранится в pet.settings_extra = {"color": "aurora", "accessories": ["🎩"]}
    # — отдельная миграция не нужна, формат расширяемый.
    # ------------------------------------------------------------------
    PET_COLORS: dict[str, tuple[str, int]] = {
        "default": ("Классический", 0),
        "golden":  ("Золотой", 150),
        "shadow":  ("Теневой", 200),
        "candy":   ("Карамельный", 120),
        "aurora":  ("Полярное сияние", 300),
    }
    PET_ACCESSORIES: dict[str, tuple[str, int]] = {
        "🎩": ("Цилиндр", 80),
        "🎀": ("Бантик", 60),
        "🕶️": ("Очки", 70),
        "👑": ("Корона", 250),
        "🧣": ("Шарф", 50),
    }

    def customization(self, pet: Pet) -> tuple[str | None, list[str]]:
        extra = pet.settings_extra or {}
        color = extra.get("color")
        if color not in self.PET_COLORS or color == "default":
            color = None
        return color, list(extra.get("accessories") or [])

    async def buy_color(self, session: AsyncSession, pet: Pet,
                        user: User, key: str) -> str:
        """Покупка/смена окраса. Возвращает текст-результат для экрана."""
        if key not in self.PET_COLORS:
            return "❌ Такой расцветки нет."
        _title, price = self.PET_COLORS[key]
        extra = dict(pet.settings_extra or {})
        if extra.get("color") == key:
            return "✅ Этот окрас уже надет."
        if user.coins < price:
            return f"🪙 Не хватает {price - user.coins} монет (окрас стоит {price})."
        user.coins -= price
        extra["color"] = key
        pet.settings_extra = extra
        await session.commit()
        return f"🎨 Новый окрас активирован! −{price} 🪙"

    async def buy_accessory(self, session: AsyncSession, pet: Pet,
                            user: User, emoji: str) -> str:
        """Покупка аксессуара; повторное нажатие снимает его (без возврата)."""
        if emoji not in self.PET_ACCESSORIES:
            return "❌ Такой штуковины нет в гардеробе."
        title, price = self.PET_ACCESSORIES[emoji]
        extra = dict(pet.settings_extra or {})
        acc = list(extra.get("accessories") or [])
        if emoji in acc:
            acc.remove(emoji)
            extra["accessories"] = acc
            pet.settings_extra = extra
            await session.commit()
            return f"🎒 Снял {emoji} {title}."
        if len(acc) >= 3:
            return "🎒 Больше трёх аксессуаров питомцу не надеть — сними лишнее."
        if user.coins < price:
            return f"🪙 Не хватает {price - user.coins} монет ({title} стоит {price})."
        user.coins -= price
        acc.append(emoji)
        extra["accessories"] = acc
        pet.settings_extra = extra
        await session.commit()
        return f"✨ {pet.name} примерил {emoji} {title}! −{price} 🪙"

    # ------------------------------------------------------------------
    # Жизненный цикл: критическое состояние → реанимация → усыновление
    # ------------------------------------------------------------------
    RECRUIT_PRICE = 200  # монет — реанимация/«новая попытка» вместо жёсткого delete

    def is_critical(self, pet: Pet) -> bool:
        """Питомец «при смерти»: здоровье на нуле И хотя бы один базовый
        показатель тоже на нуле. Пока просто 0 по одному стату — ещё можно
        спасти уходом (лечение/еда), и это мотивирует, а не бесит.

        После реанимации действует grace-период (revive_grace_until): питомец
        не считается критическим, даже если статы снова упали, — иначе игрок
        попадал бы в цикл «умер → плати» без шанса спасти уход за монеты."""
        if pet.health > 0 or min(pet.hunger, pet.happiness,
                                 pet.energy, pet.hygiene) > 0:
            return False
        grace = (pet.settings_extra or {}).get("revive_grace_until")
        if grace:
            try:
                if _aware(datetime.fromisoformat(grace)) > datetime.now(timezone.utc):
                    return False
            except (TypeError, ValueError):
                pass  # мусорное значение — считаем, что grace нет
        return True

    async def revive(self, pet: Pet) -> str:
        """Реанимация за revive_cost() монет: статы поднимаются с нуля до 30,
        болезнь снимается. Возвращает текст результата (деньги списывает
        вызывающий хендлер — он знает баланс владельца)."""
        self._apply_revive_mechanics(pet)
        return f"💖 {esc(pet.name)} откаормлен и полон надежды! Дальше — не запускай уход."

    MAX_REVIVES = 3  # жизней у питомца: после — только усыновление нового

    def revive_cost(self, pet: Pet) -> int:
        """Стоимость реанимации растёт с каждым разом: 200 → 400 → 600.
        Возвращает -1, если жизни закончились."""
        used = int((pet.settings_extra or {}).get("revives_used", 0))
        if used >= self.MAX_REVIVES:
            return -1
        return self.RECRUIT_PRICE * (used + 1)

    def _apply_revive_mechanics(self, pet: Pet) -> None:
        """Общие механики реанимации: grace-период, счётчик жизней, снятие
        штрафа скуки. Используется и платным revive, и бесплатным onboarding-revive."""
        for stat in ("hunger", "happiness", "energy", "hygiene"):
            setattr(pet, stat, clamp(30.0))
        pet.health = clamp(30.0)
        pet.sick_since = None
        extra = dict(pet.settings_extra or {})
        now = datetime.now(timezone.utc)
        extra["revived_at"] = now.isoformat()
        extra["revive_grace_until"] = (now + timedelta(minutes=30)).isoformat()
        extra["revives_used"] = int(extra.get("revives_used", 0)) + 1
        extra.pop("bored_penalty", None)  # иначе при следующем тике снова -15
        pet.settings_extra = extra

    async def free_revive_for_newbie(self, pet: Pet) -> bool:
        """Первая реанимация новичка бесплатно: если у пользователя нет монет
        на платную, но он вообще никогда не реанимировал — дарим шанс.
        Иначе «смерть» для нового игрока = отвал в первую же неделю."""
        extra = pet.settings_extra or {}
        if int(extra.get("free_revive_used", 0)) or int(extra.get("revives_used", 0)):
            return False
        self._apply_revive_mechanics(pet)
        extra = dict(pet.settings_extra or {})
        extra["free_revive_used"] = 1
        pet.settings_extra = extra
        return True

    async def archive_pet(self, session: AsyncSession, pet: Pet,
                          reason: str = "rehomed") -> None:
        """«Усыновление» питомца: карточка уходит в историю (is_archived),
        все связанные логи (кормления/прогулки/дуэли) сохраняются."""
        pet.is_archived = True
        pet.archived_at = datetime.now(timezone.utc)
        pet.archive_reason = reason
        pet.is_sleeping = False
        pet.sleep_until = None
        pet.walk_until = None
        await session.flush()

    async def adopt_new(self, session: AsyncSession, owner_tg_id: int,
                        name: str, species_code: str) -> Pet:
        """Создаёт нового питомца поверх архивного (generation+1)."""
        from app.db.models import PetSpecies
        prev_max = (await session.execute(
            select(func.max(Pet.generation)).where(Pet.user_id == owner_tg_id)
        )).scalar_one_or_none() or 0
        try:
            species = PetSpecies(species_code)
        except ValueError:
            species = PetSpecies.cat
        pet = Pet(user_id=owner_tg_id, name=name[:64], species=species,
                  generation=prev_max + 1)
        sp = SPECIES_DATA.get(species.value, SPECIES_DATA["cat"])
        for k, v in sp["start"].items():
            setattr(pet, k, v)
        session.add(pet)
        await session.flush()
        return pet

    async def history(self, session: AsyncSession,
                      owner_tg_id: int) -> list[Pet]:
        """Архивные питомцы пользователя (старые сверху вниз по поколению)."""
        rows = (await session.execute(
            select(Pet).where(Pet.user_id == owner_tg_id,
                              Pet.is_archived.is_(True))
            .order_by(Pet.generation.desc())
        )).scalars().all()
        return list(rows)

    async def add_pet_xp(self, pet: Pet, gained: int) -> list[int]:
        """Начисляет XP питомцу; возвращает список новых уровней (для эволюции)."""
        levels: list[int] = []
        pet.xp += gained
        while pet.xp >= pet_xp_needed(pet.level):
            pet.xp -= pet_xp_needed(pet.level)
            pet.level += 1
            levels.append(pet.level)
            new_stage = compute_stage(pet.level)
            if new_stage != pet.stage:
                pet.stage = new_stage
        return levels

    # ------------------------------------------------------------------
    # Рендер карточки питомца (emoji-спрайт + бары)
    # ------------------------------------------------------------------
    def render(self, pet: Pet, owner_first_name: str = "") -> str:
        mood = compute_mood(pet)
        sp = _species(pet)
        color_key, accessories = self.customization(pet)
        color_tag = "" if not color_key else f" · {self.PET_COLORS[color_key][0]}"
        acc_line = (" ".join(accessories) + " ") if accessories else ""
        sprite = acc_line + sp["emoji"] + ("✨" if mood == "great" else "")
        stage_icon = {
            PetStage.egg: "🥚", PetStage.baby: "🐣", PetStage.teen: "🐱",
            PetStage.adult: "😼", PetStage.legendary: "🐲",
        }[pet.stage]
        lines = [
            f"{stage_icon} <b>{esc(pet.name)}</b> · {sp['title']}{color_tag} {sprite}"
            + (f" · хозяин: {esc(owner_first_name)}" if owner_first_name else ""),
            f"Уровень {pet.level} · опыт {pet.xp}/{pet_xp_needed(pet.level)} "
            f"[{stat_bar(pet.xp, 6)}]",  # грубо, но мило
            "",
            f"🍎 Сытость   {stat_bar(pet.hunger)} {int(pet.hunger)}%",
            f"😊 Счастье    {stat_bar(pet.happiness)} {int(pet.happiness)}%",
            f"⚡ Энергия    {stat_bar(pet.energy)} {int(pet.energy)}%",
            f"🫧 Гигиена    {stat_bar(pet.hygiene)} {int(pet.hygiene)}%",
            f"❤️ Здоровье   {stat_bar(pet.health)} {int(pet.health)}%",
            "",
            f"💭 Настроение: {MOOD_TEXT[mood]}",
            f"📈 Характеристики: 💪{pet.strength} 🏃{pet.agility} 🧠{pet.intellect}",
        ]
        try:
            w = weather_info()
            lines.append(f"🌦️ Погода: {w['icon']} {w['name']} — {w['note']}")
            if "holiday_icon" in w:
                lines.append(f"{w['holiday_icon']} {w['holiday_note']}")
        except Exception:
            pass
        if pet.walk_until:
            lines.append("🚶 Сейчас на прогулке…")
        if self.is_critical(pet):
            # критический баннер прямо в шапке — про него нельзя
            # «случайно не заметить», а кнопки реанимации/усыновления
            # появляются на странице «Уход» (см. pet_hub(critical=True)).
            lines.insert(0, t("pet.critical_banner", name=esc(pet.name)))
            lines.insert(1, "")
        return "\n".join(lines)
