from __future__ import annotations

import random
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pet, PetStage, User
from app.i18n import t
from app.utils.formatting import (
    clamp,
    holiday_effect_mults,
    season_for,
    stat_bar,
    weather_info,
)
from app.utils.html_text import esc
from app.utils.local_time import localize
from app.utils.local_time import now as local_now

CRIT_MSG = "pet.critical_deny"

def _aware(dt: datetime) -> datetime:
    return localize(dt)

SEASON_DECAY_MULT = {
    "winter": {"energy": 1.3, "hunger": 1.2},
    "summer": {"hygiene": 1.2, "hunger": 1.1},
    "autumn": {"happy": 1.15},
}
SPRING_ALL_HAPPY_MULT = 0.8

SPECIES_SEASON_DECAY_MULT = {
    "chinchilla": {"summer": {"happy": 1.4}, "winter": {"happy": 0.7}},
}

def season_decay_mult(season: str, stat_key: str, species: str = "") -> float:
    m = SEASON_DECAY_MULT.get(season, {}).get(stat_key, 1.0)
    if season == "spring" and stat_key == "happy":
        m *= SPRING_ALL_HAPPY_MULT
    m *= SPECIES_SEASON_DECAY_MULT.get(species, {}).get(season, {}).get(stat_key, 1.0)
    return m

# Базовые скорости изменения статов (ч/час). Умножаются на видовые,
# сезонные и погодные модификаторы, а также на глобальные множители из
# настроек (app.services.balance) — там же можно переопределить значения
# без правки кода (BALANCE_* в .env).
DECAY_PER_HOUR = {
    "hunger": 4.0,
    "happiness": 2.0,
    "energy_day": 1.2,
    "energy_sleep": 8.0,   # прирост ⚡ во сне (единая правда: см. sleep_regen)
    "hygiene": 3.0,
    "health_low_care": 3.0,
}
# Награда за победу в игре (к happiness, до множителей) и штраф энергии.
PLAY_WIN_HAPPY = 12.0
PLAY_LOSE_HAPPY = 5.0

# ── Константы ухода — ЕДИНЫЙ источник правды ──────────────────────────────
# Значения механик, которые раньше были разбросаны хардкодом по телу
# методов и текстам справки/помощи (из-за чего руководство расходилось с
# кодом). Действия читают эти константы; тексты (pet_manual, HELP_TEXT,
# i18n) — строят сообщения от них же. Изменил механику — поправь число
# здесь, и все экраны подтянутся автоматически.
COOLDOWN_FEED_SEC = 60      # перерыв между кормлениями
COOLDOWN_WASH_SEC = 300     # мыться можно раз в 5 минут
COOLDOWN_PLAY_SEC = 120     # «питомец запыхался» между играми
COOLDOWN_TRAIN_SEC = 180    # перерыв между тренировками
WASH_BASE_HYGIENE = 40      # 🫧 базовый прирост гигиены за мытьё (до экипировки)
WASH_MOOD_COST = 3          # 😊 «не всякой купание в радость»: база −3 + реакция вида
HEAL_BASE_HEALTH = 35       # ❤️ базовое лечение за 💊 (множитель — от экипировки)
SICK_THRESHOLD = 50         # ниже этого здоровья питомец считается больным (mood «sick»)
SICK_RECOVER_THRESHOLD = SICK_THRESHOLD + 10  # выше этого болезнь снимается лечением
LOW_STAT_SICK_RISK = 20     # 🍎 или 🫧 ниже этого запускает риск-тик болезни ❤️
PLAY_ENERGY_MIN = 15        # ⚡ минимум для игр («питомец слишком устал»)
TRAIN_ENERGY_MIN = 20       # ⚡ минимум для тренировок
HUNGER_GRUEL_THRESHOLD = 25  # 🍎 ниже этого питомец объявляет голод
WALK_DEFAULT_HOURS = 2      # 🚶 длительность прогулки по умолчанию (UI сейчас фиксирует её)
SLEEP_DEFAULT_HOURS = 8     # 😴 план сна по умолчанию (будильник, можно разбудить раньше)
GUESS_RANGE_MAX = 20        # 🔢 верхняя граница «угадай число» (диапазон подсказки — guess_range)
SICK_CONFIRM_CHANCE_MIN = 0.05  # минимальный шанс заболеть при тике низкого здоровья
CRIT_BLOCK_NOTE = ("Блокирует обычный уход только критическое состояние "
                   "(❤️=0 И один из статов на нуле) — тогда нужна 💖 реанимация.")


def wash_hygiene_gain(svc: TamagotchiService, pet) -> int:
    """🫧 Реальный прирост гигиены за мытьё (для текстов и механики).

    Формула идентична ветке начисления в TamagotchiService.wash() — оба
    места читают WASH_BASE_HYGIENE, поэтому цифры в руководстве, i18n-
    строках и фактического эффекта не могут разойтись.
    """
    return round(WASH_BASE_HYGIENE * max(
        0.5, 1.0 + svc.gear_bonuses(pet).get("hygiene_wash_pct", 0.0)))


def wash_mood_delta(svc: TamagotchiService, pet) -> int:
    """Изменение 😊 от мытья: −WASH_MOOD_COST + реакция вида + экипировка."""
    return (-WASH_MOOD_COST + species_pref_delta(pet, "wash")
            + svc._gear_happy_flat(pet))


def heal_health_gain(svc: TamagotchiService, pet) -> int:
    """❤️ Реальный прирост здоровья за лечение (формула как в heal())."""
    return round(HEAL_BASE_HEALTH * max(
        0.5, 1.0 + svc.gear_bonuses(pet).get("heal_boost", 0.0)))


def sleep_regen_per_hour(sp: dict | None = None) -> float:
    """⚡ за час сна: база + видовой бонус (совёнок +4, шиншилла +2).

    Единая точка правды: используется и в apply_decay (тик во время сна),
    и в wake() (итог «сколько энергии набрал») — раньше они расходились,
    и текст пробуждения обещал меньше, чем реально начислялось.
    """
    bonus = float((sp or {}).get("bonus", {}).get("sleep_bonus", 0.0))
    from app.services import balance
    return (DECAY_PER_HOUR["energy_sleep"] + bonus) * balance.get_mult("sleep_regen")
PET_XP_BASE = 30.0

STAGE_BY_LEVEL = [
    (15, PetStage.legendary),
    (10, PetStage.adult),
    (6, PetStage.teen),
    (3, PetStage.baby),
    (1, PetStage.egg),
]

# SPECIES_DATA / SPECIES_START_PRICE / SPECIES_BONUS вынесены в
# app.services.pet_data (легковесный модуль для UI-справок); реэкспорт
# сохраняет обратную совместимость всех импортов.
from app.services.pet_data import SPECIES_DATA


def _species_key(pet) -> str:
    return getattr(pet.species, "value", str(pet.species))

def _species(pet) -> dict:
    return SPECIES_DATA.get(_species_key(pet), SPECIES_DATA["cat"])

def species_pref_delta(pet, action: str) -> int:
    return _species(pet)["prefers"].get(action, 0)

ACTION_LABELS = {
    "feed": "🍎 кормёжка",
    "play": "🎾 игры",
    "wash": "🫧 мытьё",
    "train": "🏋️ тренировки",
    "walk": "🚶 прогулки",
    "sleep": "😴 сон",
}

BONUS_LABELS = {
    "play_happy": "эффект игр на 😊 Счастье ×{v:g}",
    "xp_mult": "опыт (XP) ×{v:g}",
    "coin_mult": "монеты с прогулок ×{v:g}",
    "sleep_bonus": "+{v:g} ⚡ Энергии за тик сна",
}

def species_likes_text(sp: dict) -> tuple[str, str]:
    def fmt(k: str, v: int) -> str:
        name = ACTION_LABELS.get(k, k)
        sign = "+" if v > 0 else "−"
        return f"{name} {sign}{abs(v)}"

    likes = [fmt(k, v) for k, v in sp["prefers"].items() if v > 0]
    dislikes = [fmt(k, v) for k, v in sp["prefers"].items() if v < 0]
    return ", ".join(likes) or "нет", ", ".join(dislikes) or "нет"

def species_bonuses_text(sp: dict) -> str:
    parts = []
    for k, v in sp.get("bonus", {}).items():
        label = BONUS_LABELS.get(k)
        if label is None or not isinstance(v, (int, float)) or v == 0:
            continue
        neutral = (k.endswith("_mult") or k == "play_happy") and v == 1
        if neutral:
            continue
        parts.append(label.format(v=v))
    return ", ".join(parts) or "особых бонусов нет"

SET_SPECIES_SYNERGY: dict[str, dict[str, tuple[str, float]]] = {
    "dreamer": {
        "owl": ("sleep_regen", 0.35),
        "chinchilla": ("sleep_regen", 0.20),
    },
    "zen": {
        "chinchilla": ("happy_decay", -0.10),
        "dragon": ("happy_decay", -0.10),
    },
    "ranger": {
        "fox": ("walk_coin", 0.20),
    },
    "titan": {
        "dog": ("train_yield", 0.20),
    },
}

def set_species_synergy(pet, sets_worn: list[str], kind: str) -> float:
    key = _species_key(pet)
    total = 0.0
    for st in sets_worn:
        spec = SET_SPECIES_SYNERGY.get(st, {}).get(key)
        if spec and spec[0] == kind:
            total += spec[1]
    return total

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
    if pet.health < SICK_THRESHOLD:
        return "sick"
    avg = (pet.hunger + pet.happiness + pet.energy + pet.hygiene) / 4
    if pet.hunger < HUNGER_GRUEL_THRESHOLD:
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

MOOD_I18N_KEY = {
    "great": "pet.mood_great", "good": "pet.mood_good", "ok": "pet.mood_ok",
    "sad": "pet.mood_sad", "sick": "pet.mood_sick",
    "sleeping": "pet.mood_sleeping", "hungry": "pet.mood_hungry",
}

def mood_text(mood: str) -> str:
    key = MOOD_I18N_KEY.get(mood)
    return t(key) if key else MOOD_TEXT.get(mood, "")

def _mood_value(pet: Pet) -> int:
    return round((pet.hunger + pet.happiness + pet.energy + pet.hygiene) / 4)

def render_mood_line(pet: Pet, mood: str | None = None) -> str:
    mood = mood or compute_mood(pet)
    label = esc(mood_text(mood))
    return f"💭 Настроение: {label} ({_mood_value(pet)}/100)"

STYLE_ITEMS_PER_PAGE = 8

class TamagotchiService:
    def __init__(self, session: AsyncSession | None = None) -> None:
        self.session = session

    async def apply_decay(self, pet: Pet, now: datetime | None = None) -> bool:
        now = now or local_now()
        last = _aware(pet.last_update)
        hours = (now - last).total_seconds() / 3600.0
        if hours <= 0:
            return False

        changed = True
        sp = _species(pet)
        d = sp["decay"]
        try:
            from app.config import get_settings
            season = season_for(now) if get_settings().weather_enabled else ""
        except (ImportError, AttributeError) as exc:
            logger.debug(f"season lookup failed, decay without seasonality: {exc}")
            season = ""
        try:
            from app.services.weather import weather_decay_mods
            wmods = weather_decay_mods()
        except Exception:
            wmods = {}
        sp_key = _species_key(pet)
        from app.services import balance
        decay_hunger = balance.get_mult("hunger_decay") * d.get("hunger", 1.0) * season_decay_mult(season, "hunger", sp_key) * wmods.get("hunger", 1.0) * self.decay_multiplier(pet, "hunger")
        decay_happy = balance.get_mult("happy_decay") * d.get("happiness", 1.0) * season_decay_mult(season, "happy", sp_key) * wmods.get("happy", 1.0) * self.decay_multiplier(pet, "happy")
        decay_energy_day = balance.get_mult("energy_decay") * d.get("energy", 1.0) * season_decay_mult(season, "energy", sp_key) * wmods.get("energy", 1.0) * self.decay_multiplier(pet, "energy")
        decay_hygiene = balance.get_mult("hygiene_decay") * d.get("hygiene", 1.0) * season_decay_mult(season, "hygiene", sp_key) * wmods.get("hygiene", 1.0) * self.decay_multiplier(pet, "hygiene")
        sleep_regen = sleep_regen_per_hour(sp) * self.action_modifier(pet, "sleep_regen")

        was_sleeping = pet.is_sleeping
        woke_up = False
        sleep_from = _aware(pet.sleep_started_at) if was_sleeping and pet.sleep_started_at else None
        # Момент срабатывания будильника (для фазового расчёта сна и «скуки»)
        alarm = _aware(pet.sleep_until) if (was_sleeping and pet.sleep_until) else None
        # Полная длительность запланированного сна (из настроек усыновления,
        # по умолчанию 8 ч). Нужна, если sleep_started_at затёрт/утерян:
        # иначе нельзя отличить реальный сон от «проспанного» хвоста.
        planned_h = 8.0
        try:
            planned_h = float((pet.settings_extra or {}).get("sleep_hours", 8) or 8)
        except (TypeError, ValueError):
            pass
        if was_sleeping:
            # Фазовый расчёт сна: интервал [last, now] делится на часть ДО
            # будильника (это настоящий сон: ⚡ растёт по единой формуле
            # sleep_regen × часы, голод убывает ×0.5, счастье/гигиена
            # заморожены) и часть ПОСЛЕ будильника (питомец фактически уже
            # не спит — декеи идут в полную силу). Раньше весь просроченный
            # интервал (часы/сутки) целиком начислялся как сон либо energy
            # ставилась в 100 разом — оба варианта ломали баланс и
            # маскировали «скуку».
            # Граница «сна» = максимум из будильника и конца планового сна
            # (sleep_started_at + planned_h), но не позже now. Так честный
            # недосып (разбудили раньше будильника) не превращается в
            # мгновенные 100⚡. Но если питомец реально ПРОСПАЛ будильник
            # (текущий тик догоняет опоздание больше суток назад), хвост
            # после сигнала тоже засчитывается сном: иначе забытый на сутки
            # питомец терял бы счастье/гигиену за всё время «проспанного»
            # отсутствия, хотя физически лежал и спал. Просрочка ограничена
            # сверху 24 ч (за дальний горизонт отвечает штраф «скуки»).
            sleep_end = alarm
            if sleep_from is not None:
                sleep_end = max(sleep_end or datetime.min.replace(tzinfo=sleep_from.tzinfo),
                                sleep_from + timedelta(hours=planned_h))
            if sleep_end is not None and alarm is not None and now >= alarm:
                sleep_end = max(sleep_end, min(now, alarm + timedelta(hours=24)))
            if sleep_end is not None:
                sleep_end = min(sleep_end, now)
            if sleep_end is not None:
                asleep_hours = max(0.0, min((sleep_end - last).total_seconds() / 3600.0, hours))
            else:
                asleep_hours = hours
            awake_hours = max(0.0, hours - asleep_hours)
            pet.energy = clamp(pet.energy + sleep_regen * asleep_hours)
            pet.hunger = clamp(pet.hunger - decay_hunger * 0.5 * asleep_hours)
            if awake_hours > 0:
                pet.energy = clamp(pet.energy - decay_energy_day * awake_hours)
                pet.hunger = clamp(pet.hunger - decay_hunger * awake_hours)
            if alarm is not None and now >= alarm:
                # Пробуждение по будильнику. Видовые бонусы сна уже учтены
                # в sleep_regen_per_hour(); раньше сюда дополнительно
                # прибавлялся sp["bonus"]["sleep_bonus"] — двойной бонус,
                # рассогласованный с wake().
                pet.is_sleeping = False
                pet.sleep_until = None
                pet.sleep_started_at = None
                pet.happiness = clamp(pet.happiness + species_pref_delta(pet, "sleep"))
                woke_up = True
        else:
            pet.energy = clamp(pet.energy - decay_energy_day * hours)
            pet.hunger = clamp(pet.hunger - decay_hunger * hours)

        # Во сне счастье НЕ падает: сон — отдых и легальный способ
        # пережить «скуку» без штрафа (уходовая механика, а не эксплойт).
        # Гигиена во сне тоже не пачкается. При пробуждении по будильнику
        # время ПОСЛЕ сигнала будильника питомец фактически бодрствует —
        # декеи счастья/гигиены идут только на эту «проспанную» часть.
        sleeping_now = pet.is_sleeping
        if sleeping_now:
            # Всё ещё спит (будильник не сработал): счастье и гигиена
            # заморожены полностью.
            happy_hours = 0.0
            hygiene_hours = 0.0
        elif was_sleeping and woke_up:
            # Проспанный будильник: декеи только на часть ПОСЛЕ сигнала, но в
            # рамках данного тика [last, now] — иначе один большой тик
            # «догоняния» съел бы счастье за все сутки разом. Отсчёт ведётся от
            # конца фазы сна (sleep_end), а не от будильника: если питомец спал
            # дольше плана (sleep_started_at + planned_h > alarm), хвост после
            # плана — тоже сон (недобудился), и счастье там заморожено. Иначе
            # просрочка длиннее bored_hours давала бы каскад: дрейф за весь
            # «проспанный» интервал + штраф −15 в одном тике.
            awake_in_tick = hours - asleep_hours
            rest_start = sleep_end or last
            happy_hours = hygiene_hours = max(0.0, min(
                (now - rest_start).total_seconds() / 3600.0, awake_in_tick))
        else:
            happy_hours = hygiene_hours = hours
        if happy_hours > 0:
            pet.happiness = clamp(pet.happiness - decay_happy * happy_hours)
            pet.hygiene = clamp(pet.hygiene - decay_hygiene * hygiene_hours)

        # «Скука»: питомец не получал ЗАБОТЫ (кормёжка/игра/мытьё/прогулка)
        # дольше порога — однократный штраф к счастью. Раньше условие
        # строилось на hours (времени с последнего тика apply_decay), а
        # сброс флага был привязан к _set_cooldown: пассивные тики
        # планировщика сбивали его, и штраф мог как не сработать при
        # настоящем забвении, так и сработать без него. Теперь источник
        # истины — метка last_care, обновляемая только реальными действиями
        # ухода (_mark_care). После штрафа отсчёт продолжается от момента
        # штрафа (т.е. при полном молчании штраф повторяется раз в порог).
        bored_hours = float(balance.get_mult("boredom_hours"))
        last_care_iso = (pet.settings_extra or {}).get("last_care")
        flag_iso = (pet.settings_extra or {}).get("bored_penalty")
        base_dt = None
        for raw in (last_care_iso, flag_iso):
            if not raw:
                continue
            try:
                dt = _aware(datetime.fromisoformat(raw))
            except (TypeError, ValueError):
                continue
            if base_dt is None or dt > base_dt:
                base_dt = dt
        if base_dt is None:
            # Заботы ещё не было (и штрафа тоже): отсчитываем от последней
            # актуализации статов — это момент усыновления/пробуждения тика.
            base_dt = last
        # Сон — отдых: «скука» во сне НЕ копится вовсе. Точка отсчёта
        # сдвигается на ПРОБУЖДЕНИЕ (будильник), но не может убегать в
        # будущее: если питомец проспал будильник больше bored_hours, он
        # фактически столько и не получал заботы — штраф остаётся
        # законным. Поэтому wake_point = min(будильник, now - порог).
        # Каскад штрафов сразу после длинного сна при этом исключён:
        # просрочка ≤ порога прощает забытый интервал до засыпания, а
        # следующий штраф возможен только через bored_hours ПОСЛЕ пробуждения.
        slept_this_tick = bool(sleeping_now or woke_up)
        if slept_this_tick:
            wake_point = now if sleeping_now else alarm
            if wake_point is not None:
                wake_point = min(wake_point, now - timedelta(hours=bored_hours))
                if base_dt is None or wake_point > base_dt:
                    base_dt = wake_point
        if (not pet.is_sleeping
                and (now - base_dt).total_seconds() / 3600.0 >= bored_hours):
            pet.happiness = clamp(pet.happiness - balance.get_mult("boredom_penalty"))
            pet.settings_extra = {**(pet.settings_extra or {}),
                                  "bored_penalty": now.isoformat()}

        if pet.hunger < LOW_STAT_SICK_RISK or pet.hygiene < LOW_STAT_SICK_RISK:
            g = self.gear_bonuses(pet)
            hp_mult = max(0.1, 1.0 + g.get("health_decay_pct", 0.0))
            from app.services import balance
            pet.health = clamp(pet.health - balance.get_mult("health_decay") * hours * hp_mult)
            if pet.health < SICK_THRESHOLD and pet.sick_since is None:
                sick_chance = max(SICK_CONFIRM_CHANCE_MIN,
                                  1.0 + g.get("sick_chance_pct", 0.0))
                if random.random() <= sick_chance:
                    pet.sick_since = now
        elif pet.health < 100 and pet.sick_since is None:
            pet.health = clamp(pet.health + 1.0 * hours)

        walk_finished = bool(pet.walk_until) and now >= _aware(pet.walk_until)

        pet.last_update = now
        return changed or walk_finished

    def _check_cooldown(self, pet: Pet, action: str, seconds: int,
                        now: datetime) -> tuple[bool, int]:
        key = f"{action}_at"
        uses_key = f"{action}_uses"
        extra = pet.settings_extra or {}
        from app.services import balance
        if not extra.get(key):
            return True, 0
        elapsed = (now - _aware(datetime.fromisoformat(extra[key]))).total_seconds()
        # Кулдауны НЕ замораживаются во сне и не вычитаются после него.
        # Это осознанное правило баланса: иначе «спать между играми» было бы
        # эксплойтом против лимита действий (короткие сны по кругу снимали бы
        # ожидание быстрее реального времени). Сон влияет только на статы
        # (⚡ растёт, 😊/🫧 стоят, 🍎 убывает ×0.5) и на «скуку» (не копится).
        if elapsed >= seconds:
            self._reset_uses(pet, action)
            return True, 0
        if int(extra.get(uses_key, 0)) <= int(balance.get_mult("free_actions")):
            return True, 0
        return False, int(seconds - elapsed)

    @staticmethod
    def _free_use_left(pet: Pet, action: str) -> bool:
        from app.services import balance
        return int((pet.settings_extra or {}).get(f"{action}_uses", 0)) \
            <= int(balance.get_mult("free_actions"))

    @staticmethod
    def _reset_uses(pet: Pet, action: str) -> None:
        uses_key = f"{action}_uses"
        extra = pet.settings_extra or {}
        if uses_key in extra:
            new = {k: v for k, v in extra.items() if k != uses_key}
            pet.settings_extra = new

    def _set_cooldown(self, pet: Pet, action: str, now: datetime) -> None:
        uses_key = f"{action}_uses"
        extra = pet.settings_extra or {}
        uses = int(extra.get(uses_key, 0)) + 1
        pet.settings_extra = {**extra, f"{action}_at": now.isoformat(),
                              uses_key: uses}

    @staticmethod
    def _mark_care(pet: Pet, now: datetime) -> None:
        """Метка реальной заботы (кормёжка/игра/мытьё/тренировка/прогулка).

        Единственный источник истины для таймера «скуки» в apply_decay:
        пассивные тики планировщика её НЕ обновляют, в отличие от старых
        ключей <action>_at, которые сбивали штраф.
        """
        pet.settings_extra = {**(pet.settings_extra or {}),
                              "last_care": now.isoformat()}

    def _gear_happy_flat(self, pet: Pet) -> float:
        return self.gear_bonuses(pet).get("happy_gain_flat", 0.0)

    # Единая таблица запретов по состояниям. Ключ действия приходит от
    # handlers/keyboards (feed, wash, play, sleep, train, walk, duel…);
    # синонимы нормализуются в _state_action().
    # Активные действия (то, что меняет статы/состояние питомца). Всё, что
    # НЕ в этом списке — нейтральный просмотр (карточка, стиль, магазин,
    # инвентарь, друзья, история): он доступен всегда и спящему, и гуляющему.
    _ACTIVE_ACTIONS = frozenset({"feed", "wash", "sleep", "wake", "train",
                                 "heal", "medicine", "toy", "item",
                                 "game", "play", "duel", "walk", "end_walk",
                                 "revive"})
    # Сон = нет контакта: из активных действий доступно только пробуждение.
    _SLEEP_ALLOW = frozenset({"wake"})
    # На прогулке питомец вне дома: недоступны домашние процедуры, игры,
    # арена и повторная прогулка; вернуть домой («end_walk») можно.
    _WALK_DENY = _ACTIVE_ACTIONS - {"end_walk"}

    @staticmethod
    def _state_action(action: str) -> str:
        """Нормализация ключа действия для текстов отказов."""
        a = (action or "").strip().lower()
        if a == "play":              # игра/игрушка — единый текст отказа
            return "game"
        if a == "heal":              # лечение = лекарство
            return "medicine"
        return a

    def _sleeping_deny(self, action: str) -> str:
        """Единый текст отказа во сне: точечная подсказка или общий."""
        from app import i18n
        key = f"pet.sleeping_deny_{self._state_action(action)}"
        if key in i18n.STRINGS:
            return t(key)
        return t("pet.sleeping_deny")

    def sleeping_hint(self, action: str) -> str | None:
        """Точечная подсказка к отказу «питомец спит» (для handlers)."""
        from app import i18n
        key = f"pet.sleeping_deny_{self._state_action(action)}"
        if key in i18n.STRINGS:
            return t(key)
        return None

    def _walk_deny(self, pet: Pet, action: str) -> str:
        """Единый текст отказа на прогулке: точечный или общий."""
        from app import i18n
        a = self._state_action(action)
        key = f"pet.walk_deny_{a}"
        if key in i18n.STRINGS:
            return t(key, name=pet.name)
        return t("pet.walk_deny", name=pet.name)

    # 🤒 Больной питомец соблюдает постельный режим: как в жизни, игра,
    # тренировка и прогулка только ослабят его — сначала лечение (💊 heal
    # из инвентаря) и здоровый сон. Кормление, мытьё и сон остаются
    # доступными: выздоравливающему нужны силы и чистота.
    _SICK_DENY = frozenset({"play", "game", "train", "walk", "duel"})

    def is_sick(self, pet: Pet) -> bool:
        """Болен ли питомец (диагноз ставит apply_decay по порогу SICK_THRESHOLD)."""
        return getattr(pet, "sick_since", None) is not None

    def sick_deny(self, action: str) -> str:
        """Текст отказа для больного питомца (по ключу темы/i18n)."""
        from app import i18n
        key = f"pet.sick_deny_{self._state_action(action)}"
        if key in i18n.STRINGS:
            return t(key)
        return t("pet.sick_deny")

    def state_deny(self, pet: Pet, action: str, now=None) -> str | None:
        """Единый страж состояний «болезнь / сон / прогулка».

        Возвращает текст отказа или None, если действие разрешено.
        Логика запретов:
        • Болен (🤒) — постельный режим: нельзя играть, тренироваться,
          гулять и драться; нужно лечить (💊) и давать спать. Кормить,
          мыть и укладывать спать можно.
        • Спит (😴) — с питомцем вообще нет взаимодействия: нельзя кормить,
          мыть, играть, тренироваться, лечить, гулять и драться. Разрешены
          только пробуждение («wake», в т.ч. кнопка «💤 Спать» на экране
          ухода переключается в разбудить) и нейтральные просмотры
          (карточка, стиль, инвентарь, магазин, друзья, история).
        • Гуляет (🚶) — питомец вне дома: недоступны домашние процедуры
          (мытьё, сон, тренировки, лекарства, кормление, игрушки, игры),
          повторная прогулка и арена; разрешено вернуть домой («end_walk»),
          посмотреть карточку/историю и нейтральные экраны.
        Действия, не перечисленные в таблице, считаются нейтральными
        (просмотр карточки, стиля, истории и т.п.).
        """
        now = now or local_now()
        a = self._state_action(action)
        if self.is_sick(pet) and a in self._SICK_DENY:
            return self.sick_deny(a)
        if pet.is_sleeping:
            # Сон = полное отсутствие контакта: все активные действия
            # запрещены единым текстом; точечные подсказки дают handlers
            # (например «разбуди сначала» для кнопки «💤 Спать»).
            # Нейтральные просмотры (не из _ACTIVE_ACTIONS) разрешены.
            if a in self._ACTIVE_ACTIONS and a not in self._SLEEP_ALLOW:
                return self._sleeping_deny(a)
            return None
        if self.on_walk(pet, now) and a in self._WALK_DENY:
            return self._walk_deny(pet, a)
        return None

    async def feed(self, pet: Pet, effect: dict[str, float],
                   *, with_result: bool = False):
        now = local_now()
        await self.apply_decay(pet, now)

        def _out(text: str, succeeded: bool):
            # Структурированный результат вместо «успех по подстроке»:
            # вызывающий код (handlers/shop) сам решает, писать ли лог
            # действия и запускать ли FX-анимацию. Раньше успех определялся
            # поиском «Ням-ням» в тексте — хрупкая связка сервис↔i18n.
            return (text, succeeded) if with_result else text

        if self.is_critical(pet):
            return _out(t(CRIT_MSG), False)
        # Страж состояний: спящего не кормят, гуляющего дома нет.
        deny = self.state_deny(pet, "feed", now)
        if deny:
            return _out(deny, False)
        ok, wait = self._check_cooldown(pet, "feed", COOLDOWN_FEED_SEC, now)
        if not ok:
            return _out(t("pet.cooldown_feed", sec=wait), False)
        self._set_cooldown(pet, "feed", now)
        self._mark_care(pet, now)
        hol = holiday_effect_mults(now)
        gear_mult = self.action_modifier(pet, "feed")
        hunger_mult = hol.get("feed_hunger", 1.0) * gear_mult
        tastiness = sum(v for k, v in effect.items() if k == "hunger" and v > 0) * hunger_mult
        pref = species_pref_delta(pet, "feed")
        bonus_happy = max(0, pref) + (3 if tastiness >= 40 else 0) + self._gear_happy_flat(pet)
        for stat, delta in effect.items():
            if hasattr(pet, stat):
                if stat == "hunger":
                    delta *= hunger_mult
                setattr(pet, stat, clamp(getattr(pet, stat) + delta))
        if bonus_happy:
            pet.happiness = clamp(pet.happiness + bonus_happy)
        xp = int(5 * _species(pet)["bonus"]["xp_mult"] * hol.get("xp", 1.0)
                 * self.action_modifier(pet, "xp"))
        buff_defs = effect.get("buffs") or []
        if isinstance(effect.get("buff"), dict):
            buff_defs = [effect["buff"], *buff_defs]
        for b in buff_defs:
            if isinstance(b, dict) and b.get("type"):
                try:
                    self.add_buff(pet, str(b["type"]), float(b.get("mult", 1.0)),
                                  int(b.get("duration", 1800)),
                                  str(b.get("label", "")))
                except (TypeError, ValueError):
                    pass
        await self.add_pet_xp(pet, xp)
        tail = " Очень вкусно!" if pref > 0 else (" ...не восторг, но съел." if pref < 0 else "!")
        return _out(t("pet.eaten", tail=tail), True)

    async def play(self, pet: Pet, won: bool, *, with_result: bool = False):
        now = local_now()
        await self.apply_decay(pet, now)

        def _out(text: str, succeeded: bool):
            # См. feed(): структурированный результат вместо подстрок.
            return (text, succeeded) if with_result else text

        if self.is_critical(pet):
            return _out(t(CRIT_MSG), False)
        # Страж состояний: спящий не играет, гуляющего дома нет.
        deny = self.state_deny(pet, "play", now)
        if deny:
            return _out(deny, False)
        if pet.energy < PLAY_ENERGY_MIN:
            return _out(t("pet.too_tired_play"), False)
        ok, wait = self._check_cooldown(pet, "game", COOLDOWN_PLAY_SEC, now)
        if not ok:
            return _out(f"⏳ Питомец запыхался! Подожди {wait} сек.", False)
        self._set_cooldown(pet, "game", now)
        self._mark_care(pet, now)

        from app.services import balance
        sp = _species(pet)
        mult = sp["bonus"]["play_happy"] * self.action_modifier(pet, "play_happy")
        mult *= holiday_effect_mults(now).get("play_happy", 1.0)
        pref = species_pref_delta(pet, "play")
        pet.energy = clamp(pet.energy - 6)
        pet.hygiene = clamp(pet.hygiene - 5)
        xp_base = 15 if won else 8
        xp = int(xp_base * sp["bonus"]["xp_mult"] * holiday_effect_mults(now).get("xp", 1.0)
                 * self.action_modifier(pet, "xp"))
        if won:
            pet.happiness = clamp(pet.happiness + balance.get_mult("play_win") * mult
                                  + pref + self._gear_happy_flat(pet))
            await self.add_pet_xp(pet, xp)
            return _out(t("pet.won_game", xp=xp), True)
        pet.happiness = clamp(pet.happiness + balance.get_mult("play_lose") * mult
                              + pref + self._gear_happy_flat(pet))
        await self.add_pet_xp(pet, xp)
        return _out(t("pet.lost_game", xp=xp), True)

    @staticmethod
    def rps_beaten_by(hand: str) -> str:
        """Ход, КОТОРЫМ бьют указанный hand (rock beaten by paper и т.д.).

        Исторический хелпер; единая правда о правилах — в games.RPS_BEATS.
        """
        return {"rock": "paper", "paper": "scissors", "scissors": "rock"}[hand]

    def guess_range(self, pet: Pet) -> tuple[int, int]:
        half = max(3, 10 - pet.intellect // 2)
        secret = random.randint(1, GUESS_RANGE_MAX)
        lo, hi = max(1, secret - half), min(GUESS_RANGE_MAX, secret + half)
        return secret, (lo, hi)

    async def sleep(self, pet: Pet, hours: int = SLEEP_DEFAULT_HOURS) -> str:
        now = local_now()
        await self.apply_decay(pet, now)
        if self.is_critical(pet):
            return t(CRIT_MSG)
        # Страж состояний: на прогулке питомец не уснёт дома.
        deny = self.state_deny(pet, "sleep", now)
        if deny:
            return deny
        if pet.is_sleeping:
            return t("pet.already_sleeping")
        pet.is_sleeping = True
        pet.sleep_started_at = now
        pet.sleep_until = now + timedelta(hours=hours)
        # Длительность планового сна — в settings_extra: apply_decay по ней
        # отличает настоящий сон от «проспанного» хвоста после будильника,
        # даже если sleep_started_at затёрт.
        pet.settings_extra = {**(pet.settings_extra or {}), "sleep_hours": hours}
        self._set_cooldown(pet, "sleep", now)
        return t("pet.fell_asleep", time=f"{pet.sleep_until:%H:%M}")

    async def wake(self, pet: Pet) -> str:
        now = local_now()
        await self.apply_decay(pet, now)
        if not pet.is_sleeping:
            return t("pet.not_sleeping")
        slept_h = 0.0
        if pet.sleep_started_at:
            slept_h = max(0.0, (now - _aware(pet.sleep_started_at)).total_seconds() / 3600.0)
        elif pet.sleep_until:
            planned = SLEEP_DEFAULT_HOURS
            left = max(0.0, (_aware(pet.sleep_until) - now).total_seconds() / 3600.0)
            slept_h = max(0.0, planned - left)
        pet.is_sleeping = False
        pet.sleep_until = None
        pet.sleep_started_at = None
        gained = round(slept_h * sleep_regen_per_hour(sp=_species(pet)))
        return t("pet.woken", hours=f"{slept_h:.1f}".rstrip("0").rstrip("."), energy=gained)

    async def wash(self, pet: Pet, *, with_result: bool = False):
        now = local_now()
        await self.apply_decay(pet, now)

        def _out(text: str, succeeded: bool):
            # См. feed(): структурированный результат вместо подстрок.
            return (text, succeeded) if with_result else text

        if self.is_critical(pet):
            return _out(t(CRIT_MSG), False)
        # Страж состояний: во сне не купают, гуляющего негде мыть.
        deny = self.state_deny(pet, "wash", now)
        if deny:
            return _out(deny, False)
        ok, wait = self._check_cooldown(pet, "wash", COOLDOWN_WASH_SEC, now)
        if not ok:
            return _out(f"⏳ Мыться можно раз в {COOLDOWN_WASH_SEC // 60} минут "
                        f"(осталось {wait} сек).", False)
        self._set_cooldown(pet, "wash", now)
        self._mark_care(pet, now)
        gain = wash_hygiene_gain(self, pet)
        mood = wash_mood_delta(self, pet)
        pet.hygiene = clamp(pet.hygiene + gain)
        pet.happiness = clamp(pet.happiness + mood)
        xp = int(4 * _species(pet)["bonus"]["xp_mult"] * self.action_modifier(pet, "xp"))
        await self.add_pet_xp(pet, xp)
        # Честная сводка вместо зашитого «+40»: вид, боящийся воды, должен
        # видеть реальные цифры (например 🐱 кот: гигиена +40, счастье −7).
        return _out(t("pet.washed", hygiene=gain,
                      happy=("+" if mood >= 0 else "") + str(mood)), True)

    async def heal(self, pet: Pet, *, with_result: bool = False):
        now = local_now()
        await self.apply_decay(pet, now)

        def _out(text: str, succeeded: bool):
            # См. feed(): структурированный результат вместо подстрок.
            return (text, succeeded) if with_result else text

        # Страж состояний: лекарство во сне не дают, на прогулке негде лечить.
        deny = self.state_deny(pet, "heal", now)
        if deny:
            return _out(deny, False)
        if pet.sick_since is None and pet.health >= SICK_RECOVER_THRESHOLD:
            return _out(t("pet.not_sick"), False)
        gain = heal_health_gain(self, pet)
        pet.health = clamp(pet.health + gain)
        if pet.health >= SICK_RECOVER_THRESHOLD:
            pet.sick_since = None
        await self.add_pet_xp(pet, 5)
        return _out(t("pet.healed", health=gain), True)

    def on_walk(self, pet: Pet, dt=None) -> bool:
        if not getattr(pet, "walk_until", None):
            return False
        return _aware(pet.walk_until) > (dt or local_now())

    def walk_back_line(self, pet: Pet, dt=None) -> str:
        if not self.on_walk(pet, dt):
            return ""
        back = _aware(pet.walk_until)
        left_min = int((back - (dt or local_now())).total_seconds() // 60)
        return t("pet.walk_back_line", time=f"{back:%H:%M}", minutes=f"{left_min} мин")

    async def train(self, pet: Pet, stat: str, *, with_result: bool = False):
        def _out(text: str, succeeded: bool):
            # См. feed(): структурированный результат вместо подстрок.
            return (text, succeeded) if with_result else text

        for st in ("strength", "agility", "intellect"):
            if getattr(pet, st) is None:
                setattr(pet, st, 1)
        if pet.xp is None:
            pet.xp = 0
        now = local_now()
        await self.apply_decay(pet, now)
        if self.is_critical(pet):
            return _out(t(CRIT_MSG), False)
        # Страж состояний: спящий не тренируется, гуляющему негде тренироваться.
        deny = self.state_deny(pet, "train", now)
        if deny:
            return _out(deny, False)
        if stat not in ("strength", "agility", "intellect"):
            return _out("❓ Неизвестная тренировка.", False)
        if pet.energy < TRAIN_ENERGY_MIN:
            return _out("😩 Мало энергии для тренировки.", False)
        ok, wait = self._check_cooldown(pet, "train", COOLDOWN_TRAIN_SEC, now)
        if not ok:
            return _out(f"⏳ Перерыв между тренировками: {wait} сек.", False)
        self._set_cooldown(pet, "train", now)
        self._mark_care(pet, now)
        pet.energy = clamp(pet.energy - 10)
        pet.hunger = clamp(pet.hunger - 8)
        stat_pref = {"strength": "dog", "agility": ("fox", "chinchilla"),
                     "intellect": "owl"}.get(stat)
        gain = 1 + (pet.level // 5)
        if stat_pref and _species_key(pet) in (
                stat_pref if isinstance(stat_pref, tuple) else (stat_pref,)):
            gain += 1
        train_mult = self.action_modifier(pet, "train_yield")
        gain = max(1, round(gain * train_mult)
                   + int(self.gear_bonuses(pet).get("flat_train", 0)))
        setattr(pet, stat, getattr(pet, stat) + gain)
        sp = _species(pet)
        xp = int(6 * sp["bonus"]["xp_mult"]
                 * (1.2 if species_pref_delta(pet, "train") > 0 else 1.0)
                 * self.action_modifier(pet, "xp"))
        await self.add_pet_xp(pet, xp)
        label = {"strength": t("pet.train_stat"), "agility": t("pet.train_agi"),
                 "intellect": t("pet.train_int")}[stat]
        return _out(t("pet.train_done", label=label, gain=gain), True)

    async def start_walk(self, pet: Pet, hours: int = WALK_DEFAULT_HOURS, *,
                         with_result: bool = False):
        def _out(text: str, succeeded: bool):
            # См. feed(): структурированный результат вместо подстрок.
            return (text, succeeded) if with_result else text

        now = local_now()
        await self.apply_decay(pet, now)
        if self.is_critical(pet):
            return _out(t(CRIT_MSG), False)
        if pet.walk_until:
            back = _aware(pet.walk_until)
            left_min = int((back - now).total_seconds() // 60)
            return _out(t("pet.walk_already", time=f"{back:%H:%M}",
                          minutes=f"{max(0, left_min)} мин"), False)
        # Страж состояний: спящий на прогулку не идёт.
        deny = self.state_deny(pet, "walk", now)
        if deny:
            return _out(deny, False)
        pet.walk_until = now + timedelta(hours=hours)
        pet.walk_start_at = now
        pet.settings_extra = {**(pet.settings_extra or {}), "walk_hours": hours}
        self._mark_care(pet, now)
        return _out(t("pet.walk_started", hours=hours,
                      time=f"{_aware(pet.walk_until):%H:%M}"), True)

    async def end_walk(self, pet: Pet) -> str:
        now = local_now()
        await self.apply_decay(pet, now)
        if not getattr(pet, "walk_until", None):
            return t("pet.not_walking")
        if not self.on_walk(pet, now):
            text, coins, xp = self.finish_walk_event(pet)
            pet.walk_until = None
            pet.walk_start_at = None
            return f"{text}\n⏱ Питомец уже вернулся — вот итог прогулки."
        started = _aware(pet.walk_start_at) if getattr(pet, "walk_start_at", None) else None
        back = _aware(pet.walk_until)
        planned_h = float((pet.settings_extra or {}).get("walk_hours", WALK_DEFAULT_HOURS)) or WALK_DEFAULT_HOURS
        frac = 1.0
        if started is not None and back > started:
            elapsed = (now - started).total_seconds()
            total = (back - started).total_seconds()
            frac = max(0.0, min(1.0, elapsed / total))
        hours = round(frac * planned_h, 1)
        text, coins, xp = self.finish_walk_event(pet)
        if coins and frac < 1.0:
            new_c = int(coins * frac)
            if new_c != coins:
                text = text.replace(f"+{coins} монет", f"+{new_c} монет")
            coins = new_c
        if xp and frac < 1.0:
            xp = int(xp * frac)
        pet.walk_until = None
        pet.walk_start_at = None
        extra = (pet.settings_extra or {})
        if "walk_hours" in extra:
            pet.settings_extra = {k: v for k, v in extra.items() if k != "walk_hours"}
        reward_bits = []
        if coins:
            reward_bits.append(f"🪙 +{coins} монет")
        if xp:
            reward_bits.append(f"✨ +{xp} XP")
        reward = ", ".join(reward_bits) if reward_bits else "накоплено ничего 🤷"
        # t("pet.walk_returned_early", ...) уже содержит «+N монет» —
        # повторный хвост с наградой дублировал строку на экране.
        return t("pet.walk_returned_early", hours=f"{hours:.1f}".rstrip("0").rstrip("."),
                 reward=reward)

    def finish_walk_event(self, pet: Pet) -> tuple[str, int, int]:
        roll = random.random()
        sp = _species(pet)
        hol = holiday_effect_mults(local_now())
        try:
            from app.services.weather import forecast_walk_mods
            wm = forecast_walk_mods(3)
        except Exception:
            wm = {}
        w_mult = float(wm.get("mult", 1.0))
        w_happy = round(float(wm.get("happy_add", 0)))
        w_energy = round(float(wm.get("energy_add", 0)))
        w_stat = float(wm.get("stat_add", 0.0))
        w_sick = float(wm.get("sick_pct", 0.0))
        coin_mult = (sp["bonus"]["coin_mult"] * hol.get("walk_coins", 1.0)
                     * self.action_modifier(pet, "walk_coin")
                     * (1.0 + self.gear_bonuses(pet).get("coin_mult", 0.0)))
        xp_mult = sp["bonus"]["xp_mult"] * self.action_modifier(pet, "xp") \
            * (1.0 + self.gear_bonuses(pet).get("walk_xp_pct", 0.0))
        pref_bonus = species_pref_delta(pet, "walk") + self._gear_happy_flat(pet)
        if w_sick > 0 and getattr(pet, "sick_since", None) is None \
                and random.random() < max(0.0, min(0.9, w_sick
                        * (1.0 + self.gear_bonuses(pet).get("sick_chance_pct", 0.0)))):
            pet.sick_since = local_now()
            pet.health = clamp(pet.health - 10)
            return ("🤒 Прогулка удалась, но питомец ПРОМЁК/ЗАМЁРЗ на улице — "
                    "простудился! Здоровье −10, нужно лечение 💊"), 0, int(8 * xp_mult)
        grow_line = ""
        if w_stat > 0 and random.random() < w_stat:
            if random.random() < 0.5:
                pet.strength += 1
                grow_line = "\n   💪 Активные игры на улице: сила +1!"
            else:
                pet.agility += 1
                grow_line = "\n   🏃 Беготня на улице: ловкость +1!"
        energy_line = ""
        if w_energy:
            pet.energy = clamp(pet.energy + w_energy)
            energy_line = f" · ⚡ {'+' if w_energy > 0 else ''}{w_energy}"
        if roll < 0.35:
            # Баланс: награда прогулки масштабируется от её длительности —
            # раньше 2ч и 8ч давали одинаковые монеты, что делало долгие
            # прогулки строго хуже (двойной декей за те же 🪙).
            planned_h = float((pet.settings_extra or {}).get("walk_hours", WALK_DEFAULT_HOURS)) or WALK_DEFAULT_HOURS
            dur_mult = max(1.0, min(2.0, planned_h / 2.0))
            c = max(1, int(random.randint(5, 15) * coin_mult * w_mult * dur_mult))
            wl = f" ☀️ Солнечная прогулка ×{w_mult:.1f}" if w_mult > 1 else ""
            dl = f" 🕗 долгая прогулка ×{dur_mult:.1f}" if dur_mult > 1 else ""
            return (f"🪙 Нашёл монетки на прогулке! +{c} монет{wl}{dl}{energy_line}"
                    + grow_line), c, int(10 * xp_mult * w_mult * dur_mult)
        if roll < 0.45:
            pet.settings_extra = {**(pet.settings_extra or {}), "pending_friend": True}
            pet.happiness = clamp(pet.happiness + 10 + pref_bonus + w_happy)
            return ("🐾 Познакомился с другим питомцем! Счастье +" + str(10 + pref_bonus + w_happy)
                    + energy_line + grow_line
                    + "\n   (возможно, станет другом — загляни в 🐾 Друзья)"), 0, int(12 * xp_mult)
        if roll < 0.55:
            pet.happiness = clamp(pet.happiness + 10 + pref_bonus + w_happy)
            return ("🐾 Познакомился с другим питомцем! Счастье +" + str(10 + pref_bonus + w_happy)
                    + energy_line + grow_line), 0, int(12 * xp_mult)
        if roll < 0.70:
            pet.hygiene = clamp(pet.hygiene - 15)
            return "💦 Упал в лужу… Гигиена −15" + energy_line + grow_line, 0, int(8 * xp_mult)
        if roll < 0.80:
            pet.health = clamp(pet.health - 10)
            return "🤧 Простудился на ветру. Здоровье −10" + grow_line, 0, int(8 * xp_mult)
        pet.happiness = clamp(pet.happiness + 5 + pref_bonus + w_happy)
        extra = " ☀️" if w_happy >= 8 else ""
        return ("🌳 Просто хорошо погулял. Счастье +" + str(5 + pref_bonus + w_happy) + extra
                + energy_line + grow_line,
                0, int(10 * xp_mult * w_mult))

    PET_COLORS: dict[str, dict] = {
        "default": {"title": "⚪ Классический", "price": 0, "bonus": {},
                    "desc": "Природный окрас, без бонусов."},
        "golden":  {"title": "🟡 Золотой", "price": 150,
                    "bonus": {"coin_mult": 0.15},
                    "desc": "Прогулки и дела приносят +15% 🪙"},
        "shadow":  {"title": "🌑 Теневой", "price": 200,
                    "bonus": {"duel_power": 5},
                    "desc": "Сила в бою +5 (тени незаметнее)"},
        "candy":   {"title": "🟠 Карамельный", "price": 120,
                    "bonus": {"feed_bonus_pct": 0.20},
                    "desc": "Еда насыщает на +20% лучше"},
        "aurora":  {"title": "✨ Полярное сияние", "price": 300,
                    "bonus": {"sleep_regen_pct": 0.25, "happy_decay_pct": -0.10},
                    "desc": "Сон восстанавливает +25% ⚡, грусть −10%/ч"},
    }
    GEAR_SLOTS: dict[str, str] = {
        "weapon": "⚔️ Оружие", "shield": "🛡️ Щит", "body": "🧥 Тело",
        "hands": "🧤 Руки", "legs": "👢 Ноги", "trinket": "🧿 Талисман",
    }
    MAX_GEAR = len(GEAR_SLOTS)
    PET_ACCESSORIES: dict[str, dict] = {
        "🪒": {"title": "Ржавый кинжал", "slot": "weapon", "price": 60,
               "bonus": {"duel_power": 3},
               "desc": "+3 💪 в бою"},
        "🏹": {"title": "Лук следопыта", "slot": "weapon", "price": 90,
               "bonus": {"train_yield_pct": 0.15},
               "desc": "Тренировки +15%"},
        "🪓": {"title": "Дровяной топор", "slot": "weapon", "price": 120,
               "bonus": {"flat_train": 1, "walk_coin_pct": 0.10},
               "desc": "+1 к тренировке, прогулки +10% 🪙"},
        "🔨": {"title": "Кузнечный молот", "slot": "weapon", "price": 150,
               "bonus": {"duel_power": 5},
               "desc": "+5 💪 в бою"},
        "⚔️": {"title": "Клинок ветерана", "slot": "weapon", "price": 200,
               "bonus": {"duel_power": 6, "train_yield_pct": 0.10},
               "desc": "+6 💪, тренировки +10%"},
        "🪄": {"title": "Волшебная палочка", "slot": "weapon", "price": 220,
               "bonus": {"xp_pct": 0.15},
               "desc": "+15% XP во всех делах"},
        "🌙": {"title": "Лунный серп", "slot": "weapon", "price": 260,
               "bonus": {"duel_power": 7, "happy_decay_pct": -0.05},
               "desc": "+7 💪, грусть −5%/ч медленнее"},
        "☄️": {"title": "Метеоритный меч", "slot": "weapon", "price": 350,
               "bonus": {"duel_power": 10},
               "desc": "+10 💪 — вершина кузни"},
        "🔪": {"title": "Кухонный тесак", "slot": "weapon", "price": 40,
               "bonus": {"duel_power": 2, "feed_bonus_pct": 0.05},
               "desc": "+2 💪 и еда усваивается +5% (нарезано с любовью)"},
        "🗡️": {"title": "Стилет дуэлянта", "slot": "weapon", "price": 170,
                "bonus": {"duel_power": 4, "train_yield_pct": 0.10},
                "desc": "+4 💪, тренировки +10% — точность решает"},
        "🥄": {"title": "Щит из ложки", "slot": "shield", "price": 50,
               "bonus": {"health_decay_pct": -0.10},
               "desc": "Здоровье утекает на 10% медленнее"},
        "🍃": {"title": "Лист-щит", "slot": "shield", "price": 80,
               "bonus": {"sick_chance_pct": -0.20},
               "desc": "Простужается на 20% реже"},
        "🪵": {"title": "Дубовый щит", "slot": "shield", "price": 120,
               "bonus": {"health_decay_pct": -0.20},
               "desc": "Здоровье −20%/ч медленнее"},
        "🛡️": {"title": "Стальной щит", "slot": "shield", "price": 180,
               "bonus": {"duel_power": 4, "health_decay_pct": -0.10},
               "desc": "+4 💪 и чуть крепче"},
        "🪞": {"title": "Зеркальный щит", "slot": "shield", "price": 220,
               "bonus": {"duel_power": 5, "walk_coin_pct": 0.10},
               "desc": "+5 💪, прогулки +10% 🪙"},
        "❄️": {"title": "Ледяная бахрома", "slot": "shield", "price": 260,
               "bonus": {"sick_chance_pct": -0.35, "hygiene_decay_pct": -0.10},
               "desc": "Болезни −35%, грязь −10%"},
        "🌟": {"title": "Звёздный заслон", "slot": "shield", "price": 320,
               "bonus": {"duel_power": 8, "health_decay_pct": -0.15},
               "desc": "+8 💪, здоровье бережёт"},
        "🥅": {"title": "Вратарская сеть", "slot": "shield", "price": 60,
               "bonus": {"sick_chance_pct": -0.10, "duel_power": 1},
               "desc": "Отбивает простуды: −10% болезней, +1 💪"},
        "🐚": {"title": "Ракушка прилива", "slot": "shield", "price": 150,
               "bonus": {"happy_decay_pct": -0.10, "walk_coin_pct": 0.05},
               "desc": "Шум моря успокаивает: грусть −10%, 🪙+5% на прогулках"},
        "🛎️": {"title": "Щит-колокольчик", "slot": "shield", "price": 90,
               "bonus": {"happy_decay_pct": -0.15},
               "desc": "Звенит и бодрит: настроение держится на 15% дольше"},
        "🧣": {"title": "Тёплый шарф", "slot": "body", "price": 50,
               "bonus": {"energy_decay_pct": -0.15},
               "desc": "Энергия тратится на 15% медленнее"},
        "🎽": {"title": "Спортивная майка", "slot": "body", "price": 80,
               "bonus": {"train_yield_pct": 0.20},
               "desc": "Тренировки +20%"},
        "🦺": {"title": "Жилет грузчика", "slot": "body", "price": 110,
               "bonus": {"hunger_decay_pct": -0.20},
               "desc": "Сытость падает на 20% медленнее"},
        "🧥": {"title": "Пуховик странника", "slot": "body", "price": 150,
               "bonus": {"sick_chance_pct": -0.25, "energy_decay_pct": -0.10},
               "desc": "Не болеет на прогулках, экономит ⚡"},
        "👘": {"title": "Шёлковая роба", "slot": "body", "price": 180,
               "bonus": {"hygiene_decay_pct": -0.25},
               "desc": "Грязь пристаёт на 25% медленнее"},
        "🎀": {"title": "Бантик непоседы", "slot": "body", "price": 60,
               "bonus": {"play_happy_pct": 0.25},
               "desc": "Игры дают +25% счастья"},
        "🥼": {"title": "Лабораторный халат", "slot": "body", "price": 220,
               "bonus": {"xp_pct": 0.15, "heal_boost": 0.20},
               "desc": "+15% XP, лечение +20%"},
        "🪖": {"title": "Походная броня", "slot": "body", "price": 280,
               "bonus": {"duel_power": 4, "hunger_decay_pct": -0.15,
                         "energy_decay_pct": -0.10},
               "desc": "+4 💪, экономит еду и энергию"},
        "🧶": {"title": "Уютный свитер", "slot": "body", "price": 70,
               "bonus": {"happy_decay_pct": -0.12},
               "desc": "Домашнее тепло: грусть −12%/ч медленнее"},
        "🎃": {"title": "Тыквенный плащ", "slot": "body", "price": 240,
               "bonus": {"walk_coin_pct": 0.15, "play_happy_pct": 0.10},
               "desc": "Хэллоуин-настроение: 🪙+15%, игры +10% 😺"},
        "🧤": {"title": "Варежи мастера", "slot": "hands", "price": 60,
               "bonus": {"train_yield_pct": 0.10},
               "desc": "Тренировки +10%"},
        "🥊": {"title": "Боксёрские перчатки", "slot": "hands", "price": 100,
               "bonus": {"duel_power": 4},
               "desc": "+4 💪 в бою"},
        "🧲": {"title": "Магнитные рукавицы", "slot": "hands", "price": 140,
               "bonus": {"walk_coin_pct": 0.20},
               "desc": "Прогулки приносят +20% 🪙"},
        "🪢": {"title": "Верёвка силача", "slot": "hands", "price": 120,
               "bonus": {"flat_train": 1},
               "desc": "+1 к каждой тренировке"},
        "✋": {"title": "Перчатка вора", "slot": "hands", "price": 160,
               "bonus": {"walk_coin_pct": 0.15, "hygiene_decay_pct": 0.05},
               "desc": "+15% 🪙, но пачкается быстрее"},
        "🖐️": {"title": "Рукав целителя", "slot": "hands", "price": 180,
               "bonus": {"heal_boost": 0.30},
               "desc": "Лечение и витамины +30%"},
        "🫱": {"title": "Лапохват", "slot": "hands", "price": 200,
               "bonus": {"feed_bonus_pct": 0.15},
               "desc": "Еда усваивается на +15% лучше"},
        "🧻": {"title": "Мыльные варежки", "slot": "hands", "price": 90,
               "bonus": {"hygiene_wash_pct": 0.30},
               "desc": "Мытьё даёт +30% гигиены"},
        "🧹": {"title": "Лапы уборщика", "slot": "hands", "price": 55,
               "bonus": {"hygiene_decay_pct": -0.12},
               "desc": "Порядок в лапах: грязь −12%/ч медленнее"},
        "🪂": {"title": "Ловкие когти", "slot": "hands", "price": 130,
               "bonus": {"train_yield_pct": 0.12, "duel_power": 2},
               "desc": "Тренировки +12%, +2 💪 — хватка хищника"},
        "🧦": {"title": "Носки спортсмена", "slot": "legs", "price": 50,
               "bonus": {"energy_decay_pct": -0.10},
               "desc": "Экономия ⚡ 10%"},
        "👟": {"title": "Кроссовки спринтера", "slot": "legs", "price": 100,
               "bonus": {"train_yield_pct": 0.15, "energy_decay_pct": -0.05},
               "desc": "Тренировки +15%, чуть бережёт силу"},
        "🥾": {"title": "Походные ботинки", "slot": "legs", "price": 130,
               "bonus": {"walk_xp_pct": 0.25, "sick_chance_pct": -0.15},
               "desc": "XP с прогулок +25%, меньше простуд"},
        "🩰": {"title": "Пуанты балерины", "slot": "legs", "price": 150,
               "bonus": {"play_happy_pct": 0.20},
               "desc": "Игры +20% счастья"},
        "👢": {"title": "Сапоги коновала", "slot": "legs", "price": 170,
               "bonus": {"flat_train": 1, "hunger_decay_pct": -0.10},
               "desc": "+1 тренировка, голод −10%"},
        "🛼": {"title": "Роликовые коньки", "slot": "legs", "price": 200,
               "bonus": {"walk_coin_pct": 0.20},
               "desc": "Прогулки +20% 🪙"},
        "🦿": {"title": "Пружинные голенищи", "slot": "legs", "price": 240,
               "bonus": {"energy_decay_pct": -0.25},
               "desc": "Энергия −25%/ч медленнее"},
        "🌠": {"title": "Подошвы кометы", "slot": "legs", "price": 300,
               "bonus": {"walk_xp_pct": 0.30, "walk_coin_pct": 0.15},
               "desc": "Прогулки: +30% XP, +15% 🪙"},
        "🩴": {"title": "Тапочки лентяя", "slot": "legs", "price": 45,
               "bonus": {"happy_decay_pct": -0.08, "energy_decay_pct": -0.05},
               "desc": "Ничего не надо: грусть −8%, ⚡ −5%/ч медленнее"},
        "⛸️": {"title": "Ледовые коньки", "slot": "legs", "price": 160,
                "bonus": {"play_happy_pct": 0.15, "train_yield_pct": 0.10},
                "desc": "Игры +15% 😺, тренировки +10% — камчатский лёд"},
        "🧸": {"title": "Талисман-мишка", "slot": "trinket", "price": 100,
               "bonus": {"sleep_regen_pct": 0.20},
               "desc": "Сон восстанавливает +20% энергии"},
        "🔮": {"title": "Кристалл мудрости", "slot": "trinket", "price": 150,
               "bonus": {"train_yield_pct": 0.50},
               "desc": "Тренировки эффективнее на +50%"},
        "📿": {"title": "Чётки сытости", "slot": "trinket", "price": 90,
               "bonus": {"hunger_decay_pct": -0.20},
               "desc": "Сытость падает на 20% медленнее"},
        "🍀": {"title": "Клевер удачи", "slot": "trinket", "price": 120,
               "bonus": {"walk_coin_pct": 0.10, "xp_pct": 0.05},
               "desc": "Удача: +10% 🪙, +5% XP"},
        "🎩": {"title": "Цилиндр джентльмена", "slot": "trinket", "price": 80,
               "bonus": {"happy_decay_pct": -0.10},
               "desc": "Грусть падает на 10% медленнее"},
        "🎓": {"title": "Выпускная шапочка", "slot": "trinket", "price": 120,
               "bonus": {"xp_pct": 0.15},
               "desc": "+15% XP во всех активностях"},
        "👑": {"title": "Корона чемпионов", "slot": "trinket", "price": 250,
               "bonus": {"duel_power": 6, "happy_decay_pct": -0.05},
               "desc": "💪+6 в бою, счастье −5%/медленнее"},
        "🕶️": {"title": "Стильные очки", "slot": "trinket", "price": 70,
                "bonus": {"walk_coin_pct": 0.10},
                "desc": "+10% монет на прогулках"},
        "🥽": {"title": "Плавательные очки", "slot": "trinket", "price": 100,
               "bonus": {"hygiene_decay_pct": -0.25},
               "desc": "Грязь пристаёт на 25% медленнее"},
        "🧭": {"title": "Компас искателя", "slot": "trinket", "price": 180,
               "bonus": {"walk_xp_pct": 0.20, "walk_coin_pct": 0.10},
               "desc": "Прогулки: +20% XP, +10% 🪙"},
        "🪬": {"title": "Оберег от хвори", "slot": "trinket", "price": 200,
               "bonus": {"sick_chance_pct": -0.40},
               "desc": "Простуды и болезни −40%"},
        "🏆": {"title": "Кубок победителя", "slot": "trinket", "price": 300,
               "bonus": {"duel_power": 5, "happy_decay_pct": -0.10},
               "desc": "+5 💪, настроение держится"},
        "💎": {"title": "Алмаз коллекционера", "slot": "trinket", "price": 320,
               "bonus": {"coin_mult": 0.20},
               "desc": "Блестит: все монеты +20%"},
        "📜": {"title": "Древний свиток", "slot": "trinket", "price": 90,
               "bonus": {"xp_pct": 0.08},
               "desc": "Мудрость предков: +8% XP во всех делах"},
    }
    STYLE_ITEMS_PER_PAGE = 8
    STYLE_SLOT_PAGE_STEP = 6

    PET_SETS: dict[str, dict] = {
        "gladiator": {
            "title": "🗡️ Гладиатор", "items": ["⚔️", "🛡️", "🥊", "👑"],
            "bonus": {"duel_power": 12},
            "desc": "Клинок+Щит+Перчатки+Корона: +12 💪 боевой мощи"},
        "ranger": {
            "title": "🏹 Следопыт", "items": ["🏹", "🥾", "🧭"],
            "bonus": {"walk_xp_pct": 0.30, "walk_coin_pct": 0.20},
            "desc": "Лук+Ботинки+Компас: прогулки +30% XP, +20% 🪙"},
        "scholar": {
            "title": "🎓 Учёный", "items": ["🎓", "🥼", "🪄"],
            "bonus": {"xp_pct": 0.30},
            "desc": "Шапочка+Халат+Палочка: +30% опыта сверх вещей"},
        "titan": {
            "title": "🪓 Титан", "items": ["🪓", "🦺", "👢", "🪢"],
            "bonus": {"flat_train": 3, "hunger_decay_pct": -0.15},
            "desc": "Топор+Жилет+Сапоги+Канат: +3 тренировки, сытость −15%"},
        "dreamer": {
            "title": "🌙 Сновидец", "items": ["🌙", "🧦", "🧸"],
            "bonus": {"sleep_regen_pct": 0.35, "energy_decay_pct": -0.15},
            "desc": "Серп+Носки+Мишка: сон +35% ⚡, энергия бережётся "
                    "(🔗 сова: ещё +35% ко сну, шиншилла: +20%)"},
        "dandy": {
            "title": "🎩 Денди", "items": ["🎩", "👘", "🕶️", "🩰"],
            "bonus": {"happy_gain_flat": 4, "play_happy_pct": 0.20},
            "desc": "Цилиндр+Роба+Очки+Пуанты: +4 😺 за любое дело"},
        "guardian": {
            "title": "🛡️ Хранитель", "items": ["🪵", "🧥", "🪬"],
            "bonus": {"health_decay_pct": -0.30, "sick_chance_pct": -0.30},
            "desc": "Дуб+Пуховик+Оберег: здоровье и иммунитет −30% потерь"},
        "merchant": {
            "title": "🪙 Купец", "items": ["🧲", "🛼", "🍀"],
            "bonus": {"coin_mult": 0.25},
            "desc": "Рукавицы+Коньки+Клевер: все монеты +25%"},
        "gourmet": {
            "title": "🍯 Сладкоежка", "items": ["🔪", "🫱", "📿"],
            "bonus": {"feed_bonus_pct": 0.35, "hunger_decay_pct": -0.15},
            "desc": "Тесак+Лапохват+Чётки: еда усваивается +35%, сытость −15%/ч"},
        "zen": {
            "title": "🕊️ Дзен", "items": ["🧶", "🩴", "🎩"],
            "bonus": {"happy_decay_pct": -0.25, "play_happy_pct": 0.15},
            "desc": "Свитер+Тапочки+Цилиндр: грусть −25%/ч, игры +15% 😺 "
                    "(🔗 шиншилла/дракончик: грусть ещё −10%/−10%)"},
        "auroraborn": {
            "title": "❄️ Дитя сияния", "items": ["❄️", "⛸️", "🌟"],
            "bonus": {"energy_decay_pct": -0.20, "sick_chance_pct": -0.25,
                      "duel_power": 4},
            "desc": "Бахрома+Коньки+Заслон: ⚡−20%, болезни −25%, +4 💪"},
        }

    BUFF_DURATION_SEC = {"drink_energy": 3600, "food_feast": 1800}

    @staticmethod
    def _fmt_dur(sec: int) -> str:
        m = round(sec / 60)
        return f"{m} мин" if m < 60 else f"{m // 60} ч" + (f" {m % 60} мин" if m % 60 else "")

    def active_buffs(self, pet: Pet) -> dict[str, float]:
        extra = pet.settings_extra or {}
        now_ts = local_now().timestamp()
        buffs: dict[str, float] = {}
        raw = extra.get("buffs") or []
        kept = []
        for b in raw:
            try:
                if float(b.get("until", 0)) > now_ts:
                    kept.append(b)
                    buffs[b["type"]] = buffs.get(b["type"], 0.0) + float(b.get("mult", 0.0))
            except (AttributeError, TypeError, ValueError, KeyError):
                continue
        if len(kept) != len(raw):
            pet.settings_extra = {**extra, "buffs": kept}
        return buffs

    def add_buff(self, pet: Pet, buff_type: str, mult: float, duration_sec: int,
                 label: str = "") -> None:
        extra = dict(pet.settings_extra or {})
        until = local_now().timestamp() + duration_sec
        raw = list(extra.get("buffs") or [])
        same = [b for b in raw if b.get("type") == buff_type
                and float(b.get("until", 0)) > local_now().timestamp()]
        if same:
            best = max(same, key=lambda b: float(b.get("until", 0)))
            best["until"] = max(float(best.get("until", 0)), until)
            best["mult"] = max(float(best.get("mult", 0)), mult)
            best["label"] = label or best.get("label", "")
        else:
            raw.append({"type": buff_type, "mult": mult, "until": until,
                        "label": label})
        extra["buffs"] = raw[-6:]
        pet.settings_extra = extra

    def active_set_codes(self, pet: Pet) -> list[str]:
        worn = set(self.customization(pet)[1])
        return [code for code, st in self.PET_SETS.items()
                if all(e in worn for e in st["items"])]

    def gear_bonuses(self, pet: Pet) -> dict[str, float]:
        extra = pet.settings_extra or {}
        color_key = extra.get("color")
        bonuses: dict[str, float] = {}

        def add(src: dict) -> None:
            for k, v in src.items():
                bonuses[k] = bonuses.get(k, 0.0) + float(v)

        c = self.PET_COLORS.get(color_key or "")
        if c:
            add(c.get("bonus", {}))
        worn = set((extra.get("gear") or {}).values())
        for emoji in worn:
            item = self.PET_ACCESSORIES.get(emoji)
            if item:
                add(item.get("bonus", {}))
        for st in self.PET_SETS.values():
            if all(e in worn for e in st["items"]):
                add(st.get("bonus", {}))
        return bonuses

    def action_modifier(self, pet: Pet, kind: str) -> float:
        g = self.gear_bonuses(pet)
        pct_key = "sleep_regen_pct" if kind == "sleep_regen" else f"{kind}_pct"
        m = 1.0 + g.get(pct_key, 0.0)
        if kind == "feed":
            m += g.get("feed_bonus_pct", 0.0)
        if kind == "duel":
            m += g.get("duel_power", 0.0) / 50.0
        buffs = self.active_buffs(pet)
        buff_key = {"feed": "food_feast", "xp": "xp_pct", "walk_coin": "coin_pct",
                    "play_happy": "happy_pct", "sleep_regen": "energy_regen_pct",
                    "train_yield": "train_pct"}.get(kind)
        if buff_key and buff_key in buffs:
            m += buffs[buff_key]
        m += set_species_synergy(pet, self.active_set_codes(pet), kind)
        return max(0.1, m)

    def decay_multiplier(self, pet: Pet, stat_key: str) -> float:
        g = self.gear_bonuses(pet)
        m = 1.0 + g.get(f"{stat_key}_decay_pct", 0.0)
        m += set_species_synergy(pet, self.active_set_codes(pet), f"{stat_key}_decay")
        if stat_key == "energy":
            m -= self.active_buffs(pet).get("no_decay", 0.0)
        return max(0.1, m)

    def duel_power_bonus(self, pet: Pet) -> int:
        return int(self.gear_bonuses(pet).get("duel_power", 0))

    def customization(self, pet: Pet) -> tuple[str | None, list[str]]:
        extra = pet.settings_extra or {}
        color = extra.get("color")
        if color not in self.PET_COLORS or color == "default":
            color = None
        gear = extra.get("gear")
        if gear is None:
            legacy = list(extra.get("accessories") or [])[:self.MAX_GEAR]
            slots = list(self.GEAR_SLOTS.keys())
            gear = {slots[i]: e for i, e in enumerate(legacy)
                    if e in self.PET_ACCESSORIES}
        else:
            gear = dict(gear)
            migrated = False
            for k, e in list(gear.items()):
                if k not in self.GEAR_SLOTS and e in self.PET_ACCESSORIES:
                    del gear[k]
                    gear[self.PET_ACCESSORIES[e]["slot"]] = e
                    migrated = True
            if migrated:
                pet.settings_extra = {**extra, "gear": gear}
        worn = sorted({e for e in gear.values() if e in self.PET_ACCESSORIES})
        return color, worn

    def gear_map(self, pet: Pet) -> dict[str, str | None]:
        extra = pet.settings_extra or {}
        gear = extra.get("gear") or {}
        return {slot: (gear.get(slot)
                       if gear.get(slot) in self.PET_ACCESSORIES else None)
                for slot in self.GEAR_SLOTS}

    def active_sets(self, pet: Pet) -> list[str]:
        key = _species_key(pet)
        titles = []
        for code in self.active_set_codes(pet):
            title = self.PET_SETS[code]["title"]
            if key in SET_SPECIES_SYNERGY.get(code, {}):
                title += " 🔗"
            titles.append(title)
        return titles

    async def buy_color(self, session: AsyncSession, pet: Pet,
                        user: User, key: str) -> str:
        if key not in self.PET_COLORS:
            return "❌ Такой расцветки нет."
        info = self.PET_COLORS[key]
        price = info["price"]
        extra = dict(pet.settings_extra or {})
        if extra.get("color") == key:
            return "✅ Этот окрас уже активен."
        # Атомарное списание (условный UPDATE) — защита от гонки
        # параллельных тапов «двойная покупка» (см. try_spend_coins).
        from app.db.repositories import UserRepository
        if not await UserRepository(session).try_spend_coins(user.tg_id, price):
            have = max(0, int(user.coins or 0))
            return f"🪙 Не хватает {max(price - have, 1)} монет (окрас стоит {price})."
        await session.refresh(user)
        extra["color"] = key
        pet.settings_extra = extra
        await session.commit()
        bonus = info.get("desc") or ""
        return (f"🎨 Новый окрас «{info['title']}» активирован! −{price} 🪙"
                + (f"\n   Бонус: {bonus}" if bonus else ""))

    async def buy_accessory(self, session: AsyncSession, pet: Pet,
                            user: User, emoji: str) -> str:
        if emoji not in self.PET_ACCESSORIES:
            return "❌ Такой вещи нет в гардеробе."
        item = self.PET_ACCESSORIES[emoji]
        title, price, slot = item["title"], item["price"], item["slot"]
        extra = dict(pet.settings_extra or {})
        gear = dict(extra.get("gear") or {})
        owned = set(extra.get("owned") or [])
        if not extra.get("gear") and extra.get("accessories"):
            legacy = list(extra["accessories"])[:self.MAX_GEAR]
            slots = list(self.GEAR_SLOTS.keys())
            gear = {slots[i]: e for i, e in enumerate(legacy)
                    if e in self.PET_ACCESSORIES}
            owned |= set(gear.values())
        worn_emojis = set(gear.values())
        if emoji in worn_emojis:
            gear = {k: v for k, v in gear.items() if v != emoji}
            extra["gear"] = gear
            extra.pop("accessories", None)
            pet.settings_extra = extra
            await session.commit()
            return f"🎒 Снял {emoji} {title} (осталась в шкафу — наденешь бесплатно)."
        current_in_slot = gear.get(slot)
        if current_in_slot == emoji:
            return "✅ Эта вещь уже надета."
        already_owned = emoji in owned
        cost = 0 if already_owned else price
        if cost > 0:
            # Атомарное списание — без него двойной тап мог купить одну
            # вещь дважды (проверка и «user.coins -=» были неатомарны).
            from app.db.repositories import UserRepository
            if not await UserRepository(session).try_spend_coins(user.tg_id, cost):
                have = max(0, int(user.coins or 0))
                return f"🪙 Не хватает {max(cost - have, 1)} монет ({title} стоит {price})."
            await session.refresh(user)
        gear[slot] = emoji
        owned.add(emoji)
        extra["gear"] = gear
        extra["owned"] = sorted(owned)
        extra.pop("accessories", None)
        pet.settings_extra = extra
        await session.commit()
        sets_now = self.active_sets(pet)
        verb = ("достал из шкафа" if already_owned
                else f"купил и надел · −{price} 🪙")
        msg = (f"✨ {pet.name} {verb} {emoji} {title}!\n"
               f"   Бонус: {item.get('desc', '')}")
        if current_in_slot and not already_owned:
            msg += f"\n   Заменил {current_in_slot} в слоте «{self.GEAR_SLOTS[slot]}»."
        if sets_now:
            msg += "\n🔥 Активные комбо-наборы: " + ", ".join(sets_now)
        return msg

    RECRUIT_PRICE = 200

    def is_critical(self, pet: Pet) -> bool:
        # Критическое состояние = питомец «умирает»: здоровье на нуле И
        # хотя бы один базовый показатель тоже на нуле. Раньше требовалось
        # одновременное обнуление ВСЕХ четырёх статов (min(...) > 0), из-за
        # чего реанимация была практически недостижима: hygiene/happiness
        # доходят до нуля только при полном забвении на несколько суток, и
        # игрок с health=0 продолжал получать «обычный уход не поможет —
        # всё в порядке» без баннера и без доступа к реанимации.
        if pet.health > 0 or min(pet.hunger, pet.happiness,
                                 pet.energy, pet.hygiene) > 0:
            return False
        grace = (pet.settings_extra or {}).get("revive_grace_until")
        if grace:
            try:
                if _aware(datetime.fromisoformat(grace)) > local_now():
                    return False
            except (TypeError, ValueError):
                pass
        return True

    async def revive(self, pet: Pet) -> str:
        self._apply_revive_mechanics(pet)
        # «откаормлен» — опечатка; текст вынесен в i18n (pet.revive_done),
        # чтобы готическая тема могла переписать его так же, как остальные
        # строки ухода.
        return t("pet.revive_done", name=esc(pet.name))

    MAX_REVIVES = 3

    def revive_cost(self, pet: Pet) -> int:
        used = int((pet.settings_extra or {}).get("revives_used", 0))
        if used >= self.MAX_REVIVES:
            return -1
        return self.RECRUIT_PRICE * (used + 1)

    def _apply_revive_mechanics(self, pet: Pet) -> None:
        for stat in ("hunger", "happiness", "energy", "hygiene"):
            setattr(pet, stat, clamp(30.0))
        pet.health = clamp(30.0)
        pet.sick_since = None
        extra = dict(pet.settings_extra or {})
        now = local_now()
        extra["revived_at"] = now.isoformat()
        extra["revive_grace_until"] = (now + timedelta(minutes=30)).isoformat()
        extra["revives_used"] = int(extra.get("revives_used", 0)) + 1
        extra.pop("bored_penalty", None)
        pet.settings_extra = extra

    async def free_revive_for_newbie(self, pet: Pet) -> bool:
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
        pet.is_archived = True
        pet.archived_at = local_now()
        pet.archive_reason = reason
        pet.is_sleeping = False
        pet.sleep_until = None
        pet.walk_until = None
        await session.flush()

    async def adopt_new(self, session: AsyncSession, owner_tg_id: int,
                        name: str, species_code: str) -> Pet:
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
        rows = (await session.execute(
            select(Pet).where(Pet.user_id == owner_tg_id,
                              Pet.is_archived.is_(True))
            .order_by(Pet.generation.desc())
        )).scalars().all()
        return list(rows)

    async def add_pet_xp(self, pet: Pet, gained: int) -> list[int]:
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

    async def render_async(self, pet: Pet, owner_first_name: str = "") -> str:
        text = self.render(pet, owner_first_name)
        try:
            from app.services.weather import local_weather, weather_hint_block
            w = await local_weather()
            line = f"🌦️ Погода: {w['icon']} {w['name']} — {w['note']}"
            hint = weather_hint_block(pet=pet)
            if hint:
                line += "\n" + hint
            lines = text.split("\n")
            for i, ln in enumerate(lines):
                if ln.startswith("🌦️ Погода:"):
                    lines[i] = line
                    break
            else:
                for i, ln in enumerate(lines):
                    if ln.startswith("💭 Настроение:"):
                        lines.insert(i, line)
                        break
                else:
                    lines.append(line)
            if "holiday_icon" in w:
                lines.append(f"{w['holiday_icon']} {w['holiday_note']}")
            back = self.walk_back_line(pet)
            if back and not any(ln.startswith("🚶 Сейчас на прогулке") for ln in lines):
                lines.append(back)
            return "\n".join(lines)
        except Exception as exc:
            logger.debug(f"render_async weather skipped: {type(exc).__name__}: {exc}")
            return text

    def render(self, pet: Pet, owner_first_name: str = "") -> str:
        mood = compute_mood(pet)
        sp = _species(pet)
        color_key, accessories = self.customization(pet)
        color_tag = "" if not color_key else f" · {self.PET_COLORS[color_key]['title']}"
        acc_line = (" ".join(accessories) + " ") if accessories else ""
        glow = {"aurora": "🌌", "golden": "💫", "shadow": "🌪", "candy": "🍬"}.get(color_key or "", "")
        sprite = acc_line + sp["emoji"] + (glow or ("✨" if mood == "great" else ""))
        stage_icon = {
            PetStage.egg: "🥚", PetStage.baby: "🐣", PetStage.teen: "🐱",
            PetStage.adult: "😼", PetStage.legendary: "🐲",
        }[pet.stage]
        lines = [
            f"{stage_icon} <b>{esc(pet.name)}</b> · {sp['title']}{color_tag} {sprite}"
            + (f" · хозяин: {esc(owner_first_name)}" if owner_first_name else ""),
            f"Уровень {pet.level} · опыт {pet.xp}/{pet_xp_needed(pet.level)} "
            f"[{stat_bar(pet.xp, 6)}]",
            "",
            f"🍎 Сытость    {stat_bar(pet.hunger)}  {int(pet.hunger)}%",
            f"😊 Счастье     {stat_bar(pet.happiness)}  {int(pet.happiness)}%",
            f"⚡ Энергия     {stat_bar(pet.energy)}  {int(pet.energy)}%",
            f"🫧 Гигиена    {stat_bar(pet.hygiene)}  {int(pet.hygiene)}%",
            f"❤️ Здоровье   {stat_bar(pet.health)}  {int(pet.health)}%",
            "",
            f"{render_mood_line(pet, mood)}",
            f"📈 Характеристики: 💪{pet.strength} 🏃{pet.agility} 🧠{pet.intellect}",
        ]
        bonus_bits = []
        g = self.gear_bonuses(pet)
        if g.get("duel_power"):
            bonus_bits.append(f"⚔️+{int(g['duel_power'])}")
        coin_pct = g.get("coin_mult", 0.0) + g.get("walk_coin_pct", 0.0)
        if coin_pct > 0:
            bonus_bits.append(f"🪙+{round(coin_pct * 100)}%")
        if g.get("xp_pct", 0.0) > 0:
            bonus_bits.append(f"XP+{int(g['xp_pct'] * 100)}%")
        if g.get("feed_bonus_pct", 0.0) > 0:
            bonus_bits.append(f"🍎еда+{int(g['feed_bonus_pct'] * 100)}%")
        for k in ("hunger_decay_pct", "energy_decay_pct", "hygiene_decay_pct", "happy_decay_pct"):
            if g.get(k, 0.0) < 0:
                stat = {"hunger": "🍎", "energy": "⚡", "hygiene": "🫧", "happy": "😊"}[k.split("_")[0]]
                bonus_bits.append(f"{stat}−{int(abs(g[k]) * 100)}%/ч")
        if g.get("sleep_regen_pct", 0.0) > 0:
            bonus_bits.append(f"💤+{int(g['sleep_regen_pct'] * 100)}%")
        if g.get("train_yield_pct", 0.0) > 0:
            bonus_bits.append(f"🏋️+{int(g['train_yield_pct'] * 100)}%")
        if g.get("play_happy_pct", 0.0) > 0:
            bonus_bits.append(f"🎾+{int(g['play_happy_pct'] * 100)}%")
        if bonus_bits:
            lines.append("🛡 Бонусы: " + " · ".join(bonus_bits))
        sets_now = self.active_sets(pet)
        if sets_now:
            lines.append("🔥 Комбо: " + ", ".join(sets_now))
        buffs = self.active_buffs(pet)
        if buffs:
            names = {"energy_regen_pct": "⚡ Бодрость", "xp_pct": "📚 Мудрость",
                     "coin_pct": "🪙 Жадность", "happy_pct": "🎈 Праздник",
                     "food_feast": "🍽 Сытный час"}
            bl = []
            for b in ((pet.settings_extra or {}).get("buffs") or []):
                left_min = int(max(0, float(b.get("until", 0)) - local_now().timestamp()) // 60)
                if left_min > 0:
                    nm = names.get(b.get("type", ""), b.get("label") or b.get("type", "?"))
                    bl.append(f"{nm} ⏳{left_min}м")
            if bl:
                lines.append("✨ Бафы: " + " · ".join(bl))
        try:
            w = weather_info()
            lines.append(f"🌦️ Погода: {w['icon']} {w['name']} — {w['note']}")
            if "holiday_icon" in w:
                lines.append(f"{w['holiday_icon']} {w['holiday_note']}")
        except (ImportError, KeyError, TypeError) as exc:
            logger.debug(f"weather line skipped: {type(exc).__name__}: {exc}")
        if pet.walk_until:
            back = self.walk_back_line(pet)
            lines.append(back or "🚶 Сейчас на прогулке…")
        if self.is_critical(pet):
            lines.insert(0, t("pet.critical_banner", name=esc(pet.name)))
            lines.insert(1, "")
        return "\n".join(lines)
