"""📖 Гид по уходу за питомцем — экраны справки, генерируемые из кода.

Вся информация берётся из ЕДИНСТВЕННЫХ источников правды:
  * SPECIES_DATA / SPECIES_START_PRICE   — виды, предпочтения, стартовые статы;
  * DECAY_PER_HOUR + balance.get_mult()  — базовые скорости падения статов
    (в текущих глобальных настройках баланса);
  * SEASON_DECAY_MULT / SPRING_ALL_HAPPY_MULT / SPECIES_SEASON_DECAY_MULT —
    сезонные модификаторы;
  * GAME_TIPS                            — подсказки по мини-играм.

Экраны:
  manual:home          — список видов + общие ссылки;
  manual:species:<код> — карточка вида (любит/не любит, статы, тактика ухода);
  manual:stats         — как поднимать каждый показатель;
  manual:games         — как устроены игры и что они дают.
"""
from __future__ import annotations

import html as _html
import re

from app.services import balance
from app.services.pet_data import SPECIES_DATA, SPECIES_START_PRICE
from app.services.pet_social import FRIEND_MAKE_HAPPY
from app.services.tamagotchi import (
    COOLDOWN_FEED_SEC,
    COOLDOWN_PLAY_SEC,
    COOLDOWN_TRAIN_SEC,
    COOLDOWN_WASH_SEC,
    GUESS_RANGE_MAX,
    HEAL_BASE_HEALTH,
    HUNGER_GRUEL_THRESHOLD,
    LOW_STAT_SICK_RISK,
    PLAY_COST_ENERGY,
    PLAY_COST_HYGIENE,
    PLAY_ENERGY_MIN,
    SEASON_DECAY_MULT,
    SICK_RECOVER_THRESHOLD,
    SICK_THRESHOLD,
    SPECIES_SEASON_DECAY_MULT,
    SPRING_ALL_HAPPY_MULT,
    TRAIN_COST_ENERGY,
    TRAIN_COST_HUNGER,
    TRAIN_ENERGY_MIN,
    WALK_EVENT_HYGIENE,
    WASH_BASE_HYGIENE,
)


def _pref_word(delta: int) -> str:
    """Оценка предпочтения вида по той же шкале, что и _pref_line."""
    if delta >= 4:
        return "обожает"
    if delta > 0:
        return "любит"
    if delta == 0:
        return "нейтрален к"
    return "не любит"


def _walk_happy_range() -> tuple[int, int]:
    """Честный диапазон 😊 за прогулку — из веток finish_walk_event():
    «хорошо погулял» = +5, «познакомился» = +10 (плюс реакция вида)."""
    base_min = 5
    base_max = 10
    dmin = min(sp["prefers"].get("walk", 0) for sp in SPECIES_DATA.values())
    dmax = max(sp["prefers"].get("walk", 0) for sp in SPECIES_DATA.values())
    return base_min + dmin, base_max + dmax


def _profile_stat_for(stat_key: str) -> tuple[str, list[str]]:
    """Виды-профилисты по тренировке — из ЕДИНОГО источника TRAIN_PROFILE_SPECIES.

    Тот же словарь читает tamagotchi.train(), поэтому справочник физически
    не может разойтись с механикой (раньше здесь была ручная копия).
    """
    from app.services.tamagotchi import TRAIN_PROFILE_SPECIES

    labels = {"strength": "💪 силу", "agility": "🏃 ловкость",
              "intellect": "🧠 интеллект"}
    return labels[stat_key], list(TRAIN_PROFILE_SPECIES.get(stat_key, ()))


def train_gain_formula_text() -> str:
    """Формула прироста тренировки, отражающая tamagotchi.train() шаг за шагом."""
    return (f"прирост = (1 + уровень//5) +1 профильному виду, "
            f"затем ×множитель экипировки/сетов (train_yield) и +"
            f"плоский бонус снаряжения (flat_train), минимум 1; "
            f"цена — −{TRAIN_COST_ENERGY} ⚡ и −{TRAIN_COST_HUNGER} 🍎, "
            f"XP ≈ 6 × множитель вида")

PREF_LABELS = {
    "play": "🎾 Игры",
    "feed": "🍎 Кормёжка",
    "wash": "🫧 Мытьё",
    "walk": "🚶 Прогулки",
    "train": "🏋️ Тренировки",
    "sleep": "😴 Сон",
}

STAT_EMOJI = {"hunger": "🍎", "happiness": "😊", "energy": "⚡",
              "hygiene": "🫧", "health": "❤️"}

SEASON_RU = {"winter": "❄️ зиму", "spring": "🌸 весну",
             "summer": "☀️ лето", "autumn": "🍂 осень"}


def _fmt(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s or "0"


def _num(v: float) -> str:
    """Число для текста в HTML-сообщениях.

    Дробные форматируются через _fmt (десятичный разделитель — точка),
    целые выводятся как есть. Нужен вместо прямого f-строчного вывода
    float: при локализации/случайной запятой текст вида «4,0 … (<20),
    иначе» ронял парсер HTML у Telegram («Unsupported start tag "20),"»),
    и карточки котёнка/щенка не открывались вовсе.
    """
    if v == int(v):
        return str(int(v))
    return _fmt(v)


def sanitize_html(text: str) -> str:
    """Гарант целостности HTML перед отправкой в Telegram.

    Любой одиночный «<», который Telegram принял бы за начало тега
    (например «здоровью (<20), иначе»), экранируется. Легальные теги
    (буква или «/» сразу после «<») остаются нетронутыми.
    """
    return re.sub(r"<(?![a-zA-Z/])", "&lt;", text)


def _pref_line(delta: int) -> str:
    if delta >= 4:
        word = "обожает"
    elif delta > 0:
        word = "любит"
    elif delta == 0:
        word = "нейтрален к"
    else:
        word = "не любит"
    sign = f"+{delta}" if delta > 0 else str(delta)
    return word, ("—" if delta == 0 else sign)


# ── Общие разделы ──────────────────────────────────────────────────────────

def stats_guide_text() -> str:
    """Как устроен каждый показатель и чем его поднимать.

    Все числа выводятся из единых источников правды: snapshot() баланса,
    константы tamagotchi (цены/кулдауны действий) и SPECIES_DATA — никаких
    литералов, способных разойтись с механикой.
    """
    d = balance.snapshot()
    w_lo, w_hi = _walk_happy_range()
    L: list[str] = ["📊 <b>Показатели питомца</b>", "",
                    "Настроение = среднее четырёх статов (🍎 😊 ⚡ 🫧). "
                    "Все они постепенно падают — задача владельца не дать "
                    "им опуститься ниже критических порогов.", ""]

    L.append(f"<b>🍎 Сытость</b> — падает ~{_num(d['hunger_decay'])}/час.")
    L.append(f"  Поднимают: кормёжка из магазина ({COOLDOWN_FEED_SEC} с между приёмами).")
    L.append("  Вкусная еда (🍰 тортик, 🍩 пончик) даёт бонус к счастью;")
    L.append(f"  ниже {HUNGER_GRUEL_THRESHOLD} питомец объявляет голод, "
             f"ниже {LOW_STAT_SICK_RISK} начинает болеть ❤️.")
    L.append(f"  Тренировка тратит −{TRAIN_COST_HUNGER} 🍎.")
    L.append("")
    L.append(f"<b>😊 Счастье</b> — самое «медленное»: ~{_num(d['happy_decay'])}/час,")
    L.append("  но его сильнее всего меняют вид, сезон и погода.")
    L.append("  Поднимают: 🎾 игры (победа ≈ +"
             + _num(d["play_win"]) + " × множитель вида), прогулки (+"
             + str(w_lo) + "…" + str(w_hi) + " за")
    L.append("  события), вкусная еда, друзья (+" + str(FRIEND_MAKE_HAPPY)
             + " за знакомство),")
    sleep_fans = [sp["emoji"] for sp in SPECIES_DATA.values()
                  if sp["bonus"]["sleep_bonus"] > 0
                  or sp["prefers"].get("sleep", 0) > 0]
    L.append("  сон для " + "/".join(sleep_fans) + ". Снижают: мытьё у")
    wash_haters = [sp["emoji"] for sp in SPECIES_DATA.values()
                   if sp["prefers"].get("wash", 0) <= -2]
    L.append("  водобоязненных (" + "/".join(wash_haters) + "), дождь, скука (−"
             + _num(d["boredom_penalty"]) + " через")
    L.append(f"  {_num(d['boredom_hours'])} ч без заботы).")
    L.append("  ⚠️ Играйте даже когда «проигрываете» — за поражение тоже")
    L.append(f"  начисляется +{_num(d['play_lose'])} 😊.")
    L.append("")
    L.append(f"<b>⚡ Энергия</b> — падает ~{_num(d['energy_decay'])}/час днём.")
    L.append("  Восстанавливает только сон (~"
             + _num(d["sleep_regen"]) + "/час; виды с бонусом сна — "
             + ", ".join(f"{sp['emoji']} +{_num(sp['bonus']['sleep_bonus'])} ⚡/ч"
                         for sp in SPECIES_DATA.values()
                         if sp["bonus"]["sleep_bonus"] > 0) + ").")
    L.append(f"  Ниже {PLAY_ENERGY_MIN} — игры недоступны, "
             f"ниже {TRAIN_ENERGY_MIN} — тренировки.")
    L.append(f"  Тратят: игра −{PLAY_COST_ENERGY} ⚡, тренировка "
             f"−{TRAIN_COST_ENERGY} ⚡.")
    L.append("")
    L.append(f"<b>🫧 Гигиена</b> — падает ~{_num(d['hygiene_decay'])}/час.")
    L.append(f"  Поднимает 🫧 мытьё (+{WASH_BASE_HYGIENE}, раз в "
             f"{COOLDOWN_WASH_SEC // 60} минут). Ниже {LOW_STAT_SICK_RISK} — риск болезни.")
    L.append(f"  Тратят: игра −{PLAY_COST_HYGIENE} 🫧, лужа на прогулке "
             f"−{WALK_EVENT_HYGIENE} 🫧.")
    dirt_slow = [sp["emoji"] for sp in SPECIES_DATA.values()
                 if sp["decay"].get("hygiene", 1.0) < 0.9]
    dirt_fast = [sp["emoji"] for sp in SPECIES_DATA.values()
                 if sp["decay"].get("hygiene", 1.0) >= 1.2]
    L.append("  Пачкается медленнее всех: " + "/".join(dirt_slow)
             + "; быстрее: " + "/".join(dirt_fast) + ".")
    L.append("")
    L.append(f"<b>❤️ Здоровье</b> — тикает вниз ({_num(d['health_decay'])}/час) только")
    L.append(f"  когда 🍎 или 🫧 ниже {LOW_STAT_SICK_RISK}. При health &lt; "
             f"{SICK_THRESHOLD} питомец может заболеть 🤒 (не мгновенно —")
    L.append("  с каждым часом растёт шанс), тогда нужна 💊 аптечка.")
    L.append(f"  Лечение снимает болезнь при health ≥ {SICK_RECOVER_THRESHOLD}")
    L.append(f"  (+{HEAL_BASE_HEALTH} ❤️ за аптечку). Больной питомец не играет,")
    L.append("  не тренируется и не гуляет, пока его не вылечишь.")
    L.append("")
    L.append("🛟 <b>Страховка от забвения:</b> если питомец не получал заботу")
    L.append(f"  более {_num(d['boredom_hours'])} часов — однократный штраф")
    L.append(f"  −{_num(d['boredom_penalty'])} 😊 («скука»). Корми, играй, гуляй —")
    L.append("  и таймер обнулится.")
    return sanitize_html("\n".join(L))


GAME_TIPS: list[tuple[str, str]] = [
    ("🔢 Угадай число",
     f"Число 1–{GUESS_RANGE_MAX}. Интеллект питомца сужает диапазон "
     "подсказки — качай 🧠"),
    ("✂️ Камень-ножницы-бумага",
     "Классика против питомца. Исход идёт в зачёт игры (победа/поражение)."),
    ("🃏 Двадцать одно",
     "Блэкджек: больше очков — победа. Здесь тоже помогает интеллект."),
]


def games_guide_text() -> str:
    d = balance.snapshot()
    L: list[str] = ["🎮 <b>Мини-игры</b>", "",
                    "Любая игра вызывает то же действие, что и кнопка «Играть»: ",
                    "победа → +" + _num(d["play_win"]) + " 😊 × множитель вида, "
                    "поражение → +" + _num(d["play_lose"]) + " 😊.",
                    "Проигрывать НЕ страшно — счастье растёт в любом исходе,",
                    "плюс XP питомцу.", "",
                    f"⏱ Кулдаун {COOLDOWN_PLAY_SEC} секунд — дальше по кнопочке",
                    "«Играть» питомец скажет «запыхался» и попросит подождать.",
                    f"⚡ Нужно минимум {PLAY_ENERGY_MIN} энергии; игра также тратит",
                    f"{PLAY_COST_ENERGY} ⚡ и {PLAY_COST_HYGIENE} 🫧 — после игры "
                    "полезно помыть.", "",
                    "Множители счастья за победу по видам:"]
    for _code, sp in SPECIES_DATA.items():
        m = sp["bonus"]["play_happy"]
        note = " 🐱 любимец игр" if m > 1.15 else (" ⚠️ не любит игры" if m < 1 else "")
        L.append(f"  {sp['emoji']} {sp['title']}: ×{_num(m)}{note}")
    L += ["", "<b>Ассортимент:</b>"]
    for name, tip in GAME_TIPS:
        L.append(f"  {name} — {tip}")
    # Праздничные эффекты — из HOLIDAY_EFFECTS (единый источник правды),
    # а не из застывшей строки про 14 февраля.
    from app.utils.formatting import HOLIDAY_EFFECTS

    valentine = HOLIDAY_EFFECTS.get((2, 14), {})
    hol_mult = valentine.get("play_happy")
    if hol_mult and hol_mult != 1.0:
        L += ["", f"💘 14 февраля все игры дают счастье ×{_num(hol_mult)}."]
    return sanitize_html("\n".join(L))


# ── Карточка вида ──────────────────────────────────────────────────────────

def species_season_notes(code: str) -> list[str]:
    """Сезонные поправки к падению статов — честно из SEASON_DECAY_MULT.

    Множитель >1 = стат «тратится» быстрее; <1 = медленнее. Весна отдельно:
    её коэффициент happiness живёт в SPRING_ALL_HAPPY_MULT, а не в словаре.
    """
    notes: list[str] = []
    for season, mods in SEASON_DECAY_MULT.items():
        for stat_key, mult in sorted(mods.items()):
            label = STAT_EMOJI.get({"happy": "happiness"}.get(stat_key, stat_key),
                                   stat_key)
            verdict = "быстрее" if mult > 1 else "медленнее"
            notes.append(f"{SEASON_RU[season]}: {label} падает {verdict} "
                         f"(×{_num(mult)}, у всех)")
    _sp_m = SPRING_ALL_HAPPY_MULT
    notes.append(f"🌸 весну: 😊 грустнеет {'быстрее' if _sp_m > 1 else 'медленнее'} "
                 f"(×{_num(_sp_m)}, у всех)")
    for season, mods in sorted(SPECIES_SEASON_DECAY_MULT.get(code, {}).items()):
        for stat_key, mult in sorted(mods.items()):
            label = STAT_EMOJI.get({"happy": "happiness"}.get(stat_key, stat_key),
                                   stat_key)
            verdict = f"{label} тратится быстрее" if mult > 1 \
                else f"{label} бережётся"
            notes.append(f"{SEASON_RU[season]} (этот вид): ×{_num(mult)} — {verdict}")
    return notes


def tactics_for(code: str, sp: dict) -> list[str]:
    """Персональные советы: что делать именно этому виду.

    Числа (кулдауны, цены, пороги, списки профилистов и редких видов) —
    из констант механики и SPECIES_DATA, а не из литералов текста.
    """
    prefers = sp["prefers"]
    bonus = sp["bonus"]
    decay = sp["decay"]
    t: list[str] = []
    if prefers.get("play", 0) > 0 and bonus["play_happy"] >= 1.0:
        t.append(f"🎾 Играй часто (кулдаун {COOLDOWN_PLAY_SEC // 60} мин) — "
                 "главный источник 😊 для него.")
    if bonus["play_happy"] < 1.0 or prefers.get("play", 0) < 0:
        t.append("🎾 Игры он любит умеренно: не трать кулдауны зря, лучше другие")
        t.append("   занятия — там доход выше.")
    w_lo, w_hi = _walk_happy_range()
    if prefers.get("walk", 0) >= 4:
        t.append("🚶 Прогулки — его конёк: чаще запускай, лови события «друг» и")
        t.append(f"   «хорошо погулял» (+{w_lo}…+{w_hi} 😊).")
    if prefers.get("walk", 0) < 0:
        t.append("🚶 На прогулках скучает (−" + str(-prefers["walk"]) + " 😊) — используй")
        t.append("   их ради монет/XP, а счастье набирай иначе.")
    if prefers.get("wash", 0) >= 4:
        t.append("🫧 Чистюля: мытьё даёт ему бонус к 😊 — мой смело, гигиена")
        t.append("   всё равно падает медленно, держи её высокой ради здоровья.")
    if prefers.get("wash", 0) <= -2:
        t.append("⛲ Воды не любит: после мытья −" + str(-prefers["wash"]) + " 😊.")
        # Скобки вокруг числа обязательны: «(<20), иначе» Telegram-парсер
        # HTML принимает за открывающий тег («Unsupported start tag "20),"»).
        t.append(f"   Мой только когда 🫧 угрожает здоровью "
                 f"(ниже ({_num(LOW_STAT_SICK_RISK)})) — иначе терпи.")
    if prefers.get("train", 0) >= 4:
        # Динамический список профилистов: берём из ЗЕРКАЛА train()
        # (_profile_stat_for), поэтому текст не может разойтись с кодом.
        profs = []
        for key in ("strength", "agility", "intellect"):
            label, codes = _profile_stat_for(key)
            if code in codes:
                profs.append(label)
        who = ", ".join(f"{SPECIES_DATA[c]['emoji']} {SPECIES_DATA[c]['title']}"
                        for c in sorted({c for _, cds in
                                         ((_profile_stat_for(k)[0],
                                           _profile_stat_for(k)[1])
                                          for k in ("strength", "agility",
                                                    "intellect"))
                                         for c in cds}))
        t.append(f"🏋️ Тренировки — его стихия: цена −{TRAIN_COST_ENERGY} ⚡ / "
                 f"−{TRAIN_COST_HUNGER} 🍎, перерыв {COOLDOWN_TRAIN_SEC // 60} мин.")
        t.append(f"   Его профильные стат(ы): {'; '.join(profs) or 'нет'}.")
        t.append(f"   Профилисты по всем тренировкам: {who}.")
        t.append(f"   Формула прироста: {train_gain_formula_text()}.")
    if bonus["sleep_bonus"] > 0:
        t.append(f"😴 Сон — его топливо (+{_num(bonus['sleep_bonus'])} ⚡/ч сверх нормы):")
        t.append("   спи чаще, энергии хватает на больше активностей в день.")
    if decay.get("energy", 1.0) >= 1.2:
        t.append(f"⚡ Быстро устаёт — следи за энергией, не давай ей упасть "
                 f"ниже {TRAIN_ENERGY_MIN}.")
    if decay.get("hygiene", 1.0) >= 1.2:
        t.append("🫧 Пачкается быстро — закладывай мытьё в рутину заранее.")
    if decay.get("happiness", 1.0) >= 1.2:
        t.append("😊 Грустит быстрее обычного — заходи хотя бы раз в несколько часов,")
        t.append(f"   иначе поймаешь штраф за скуку (−{int(balance.get_mult('boredom_penalty'))}).")
    if bonus["coin_mult"] > 1.0:
        t.append(f"🪙 Добытчик: монеты с прогулок ×{_num(bonus['coin_mult'])} —")
        t.append("   зарабатывай на экипировку именно им.")
    if bonus["xp_mult"] > 1.0:
        t.append(f"✨ Учётся быстро: XP ×{_num(bonus['xp_mult'])} — расти выше других.")
    rarest_price = max(SPECIES_START_PRICE.values())
    is_rarest = SPECIES_START_PRICE.get(code) == rarest_price and rarest_price > 0
    if is_rarest:
        t.append(f"🐉 Редчайший вид ({rarest_price} 🪙): универсал во всём, но капризен —")
        t.append("   компенсируй быстрым спадом 😊 экипировкой (см. ниже).")
    return t


def gear_tips_for(code: str, sp: dict) -> list[str]:
    """Рекомендации экипировки под слабые места вида.

    Проценты берутся из PET_ACCESSORIES / PET_SETS (TamagotchiService),
    а не из памяти автора текста: изменишь баланс вещи — справка обновится.
    """
    from app.services.tamagotchi import TamagotchiService

    acc = TamagotchiService.PET_ACCESSORIES
    sets = TamagotchiService.PET_SETS

    def _pct(v: float) -> str:
        p = round(v * 100)
        return f"+{p}%" if p > 0 else f"{p}%"

    tips: list[str] = []
    if sp["bonus"]["play_happy"] >= 1.1:
        play_boosters = [f"{e} {a['title']} ({_pct(a['bonus']['play_happy_pct'])} 😊)"
                         for e, a in acc.items()
                         if a["bonus"].get("play_happy_pct", 0) >= 0.15]
        if play_boosters:
            tips.append("🎾 Усиливает и без того сильные игры: "
                        + ", ".join(play_boosters))
    if sp["decay"].get("happiness", 1.0) >= 1.2:
        calm_sets = [f"{s['title']} ({_pct(s['bonus']['happy_decay_pct'])}/ч 😊)"
                     for s in sets.values()
                     if s["bonus"].get("happy_decay_pct", 0) <= -0.15]
        if calm_sets:
            tips.append("Замедляют падение 😊: " + ", ".join(calm_sets))
    if sp["bonus"]["play_happy"] < 1.0:
        zen = sets.get("zen")
        if zen:
            tips.append(f"{zen['title']} — снижает скорость грусти "
                        f"({_pct(zen['bonus']['happy_decay_pct'])}/ч), "
                        "чтобы реже нужны были игры")
    if sp["decay"].get("hygiene", 1.0) >= 1.2:
        tips.append("🧼 Что-то на +гигиену из инвентаря, чтобы реже мыть "
                    f"(мытьё: база +{WASH_BASE_HYGIENE} 🫧, раз в "
                    f"{COOLDOWN_WASH_SEC // 60} мин — не всем в радость)")
    if sp["prefers"].get("walk", 0) >= 4:
        def _coin_bonus(s: dict) -> float:
            return sum(v for k, v in s["bonus"].items()
                       if k in ("walk_coin_pct", "coin_mult")
                       and isinstance(v, float))

        coin_sets = [f"{s['title']} ({_pct(_coin_bonus(s))} 🪙)"
                     for s in sets.values()
                     if _coin_bonus(s) > 0]
        tips.append("🪙 Его главная ферма — прогулки; усили монеты: "
                    + ", ".join(coin_sets[:3]))
    return tips


def species_text(code: str) -> str | None:
    sp = SPECIES_DATA.get(code)
    if not sp:
        return None
    e = _html.escape
    L: list[str] = [f"{sp['emoji']} <b>{e(sp['title'])}</b>", "", e(sp["desc"]), ""]

    price = SPECIES_START_PRICE.get(code, 0)
    L.append(f"💰 Цена: {'бесплатно' if price == 0 else str(price) + ' монет'}")
    st = sp["start"]
    L.append(f"📈 Стартовые статы: 💪 {st['strength']} · 🦋 {st['agility']} · 🧠 {st['intellect']}")
    b = sp["bonus"]
    L.append(f"🎾 Игры: ×{_num(b['play_happy'])} 😊 · ✨ XP: ×{_num(b['xp_mult'])}"
             f" · 🪙 Монеты: ×{_num(b['coin_mult'])} · 😴 Сон: +{_num(b['sleep_bonus'])} ⚡/ч")
    L.append("")

    L.append("<b>Что любит и не любит</b>")
    order = ["play", "feed", "wash", "walk", "train", "sleep"]
    for key in order:
        delta = sp["prefers"].get(key, 0)
        if key == "sleep" and delta == 0 and b["sleep_bonus"] == 0:
            continue
        word, sign = _pref_line(int(delta))
        L.append(f"  {PREF_LABELS[key]}: {word} ({sign})")
    L.append("")

    L.append("<b>Характер спада статов (относительно нормы)</b>")
    norm = {"hunger": "🍎", "happiness": "😊", "energy": "⚡", "hygiene": "🫧"}
    bits = []
    for k, lbl in norm.items():
        m = sp["decay"].get(k, 1.0)
        v = balance.get_mult({"hunger": "hunger_decay", "happiness": "happy_decay",
                              "energy": "energy_decay", "hygiene": "hygiene_decay"}[k])
        eff = v * m
        mark = "" if abs(m - 1.0) < 0.01 else (" ⬆" if m > 1 else " ⬇")
        bits.append(f"{lbl} {_num(eff)}/ч{mark}")
    L.append("  " + " · ".join(bits))
    L.append("")

    notes = species_season_notes(code)
    if notes:
        L.append("<b>Сезоны</b>")
        seen = set()
        for n in sorted(notes):
            if n in seen:
                continue
            seen.add(n)
            L.append(f"  • {n}")
        L.append("")

    L.append("<b>Тактика ухода за этим видом</b>")
    for i, tip in enumerate(tactics_for(code, sp), 1):
        L.append(f"  {i}. {tip}" if i == 1 else f"  {tip}")
    gt = gear_tips_for(code, sp)
    if gt:
        L.append("")
        L.append("<b>Полезная экипировка</b>")
        for g in gt:
            L.append(f"  • {g}")
    return sanitize_html("\n".join(L))


def home_text() -> str:
    L = ["📖 <b>Гид по уходу за питомцем</b>", "",
         "Здесь живая справка — она всегда показывает актуальные цифры из",
         "настроек баланса, а не застывший текст. Выбери тему:", ""]
    return sanitize_html("\n".join(L))
