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

from app.services import balance
from app.services.pet_data import SPECIES_DATA, SPECIES_START_PRICE
from app.services.tamagotchi import (
    COOLDOWN_FEED_SEC,
    COOLDOWN_PLAY_SEC,
    COOLDOWN_WASH_SEC,
    GUESS_RANGE_MAX,
    HEAL_BASE_HEALTH,
    HUNGER_GRUEL_THRESHOLD,
    LOW_STAT_SICK_RISK,
    PLAY_ENERGY_MIN,
    SEASON_DECAY_MULT,
    SICK_RECOVER_THRESHOLD,
    SICK_THRESHOLD,
    SPECIES_SEASON_DECAY_MULT,
    SPRING_ALL_HAPPY_MULT,
    TRAIN_ENERGY_MIN,
    WASH_BASE_HYGIENE,
)

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
    """Как устроен каждый показатель и чем его поднимать."""
    d = balance.snapshot()
    L: list[str] = ["📊 <b>Показатели питомца</b>", "",
                    "Настроение = среднее четырёх статов (🍎 😊 ⚡ 🫧). "
                    "Все они постепенно падают — задача владельца не дать "
                    "им опуститься ниже критических порогов.", ""]

    L.append(f"<b>🍎 Сытость</b> — падает ~{_fmt(d['hunger_decay'])}/час.")
    L.append(f"  Поднимают: кормёжка из магазина ({COOLDOWN_FEED_SEC} с между приёмами).")
    L.append("  Вкусная еда (🍰 тортик, 🍩 пончик) даёт бонус к счастью;")
    L.append(f"  ниже {HUNGER_GRUEL_THRESHOLD} питомец объявляет голод, "
             f"ниже {LOW_STAT_SICK_RISK} начинает болеть ❤️.")
    L.append("")
    L.append(f"<b>😊 Счастье</b> — самое «медленное»: ~{_fmt(d['happy_decay'])}/час,")
    L.append("  но его сильнее всего меняют вид, сезон и погода.")
    L.append("  Поднимают: 🎾 игры (победа ≈ +"
             + _fmt(d["play_win"]) + " × множитель вида), прогулки (+5…+10 за")
    L.append("  события), вкусная еда, друзья (+3 за знакомство, +1/день),")
    L.append("  сон для сов/шиншилл. Снижают: мытьё у водобоязненных, жара")
    L.append(f"  для шиншиллы, дождь, скука (−{_fmt(d['boredom_penalty'])} через")
    L.append(f"  {_fmt(d['boredom_hours'])} ч без заботы).")
    L.append("  ⚠️ Играйте даже когда «проигрываете» — за поражение тоже")
    L.append(f"  начисляется +{_fmt(d['play_lose'])} 😊.")
    L.append("")
    L.append(f"<b>⚡ Энергия</b> — падает ~{_fmt(d['energy_decay'])}/час днём.")
    L.append("  Восстанавливает только сон (~"
             + _fmt(d["sleep_regen"]) + "/час, у совёнка и шиншиллы быстрее).")
    L.append(f"  Ниже {PLAY_ENERGY_MIN} — игры недоступны, "
             f"ниже {TRAIN_ENERGY_MIN} — тренировки.")
    L.append("")
    L.append(f"<b>🫧 Гигиена</b> — падает ~{_fmt(d['hygiene_decay'])}/час.")
    L.append(f"  Поднимает 🫧 мытьё (+{WASH_BASE_HYGIENE}, раз в "
             f"{COOLDOWN_WASH_SEC // 60} минут). Ниже {LOW_STAT_SICK_RISK} — риск болезни.")
    L.append("  У шиншиллы пачкается вдвое медленнее, у щенка/лисёнка — быстрее.")
    L.append("")
    L.append(f"<b>❤️ Здоровье</b> — тикает вниз ({_fmt(d['health_decay'])}/час) только")
    L.append(f"  когда 🍎 или 🫧 ниже {LOW_STAT_SICK_RISK}. При health &lt; "
             f"{SICK_THRESHOLD} питомец может заболеть 🤒 (не мгновенно —")
    L.append("  с каждым часом растёт шанс), тогда нужна 💊 аптечка.")
    L.append(f"  Лечение снимает болезнь при health ≥ {SICK_RECOVER_THRESHOLD}")
    L.append(f"  (+{HEAL_BASE_HEALTH} ❤️ за аптечку). Больной питомец не играет,")
    L.append("  не тренируется и не гуляет, пока его не вылечишь.")
    L.append("")
    L.append("🛟 <b>Страховка от забвения:</b> если питомец не получал заботу")
    L.append(f"  более {_fmt(d['boredom_hours'])} часов — однократный штраф")
    L.append(f"  −{_fmt(d['boredom_penalty'])} 😊 («скука»). Корми, играй, гуляй —")
    L.append("  и таймер обнулится.")
    return "\n".join(L)


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
                    "победа → +" + _fmt(d["play_win"]) + " 😊 × множитель вида, "
                    "поражение → +" + _fmt(d["play_lose"]) + " 😊.",
                    "Проигрывать НЕ страшно — счастье растёт в любом исходе,",
                    "плюс XP питомцу.", "",
                    f"⏱ Кулдаун {COOLDOWN_PLAY_SEC} секунд — дальше по кнопочке",
                    "«Играть» питомец скажет «запыхался» и попросит подождать.",
                    f"⚡ Нужно минимум {PLAY_ENERGY_MIN} энергии; игра также тратит",
                    "6 ⚡ и 5 🫧 — после игры полезно помыть.", "",
                    "Множители счастья за победу по видам:"]
    for _code, sp in SPECIES_DATA.items():
        m = sp["bonus"]["play_happy"]
        note = " 🐱 любимец игр" if m > 1.15 else (" ⚠️ не любит игры" if m < 1 else "")
        L.append(f"  {sp['emoji']} {sp['title']}: ×{_fmt(m)}{note}")
    L += ["", "<b>Ассортимент:</b>"]
    for name, tip in GAME_TIPS:
        L.append(f"  {name} — {tip}")
    L += ["", "💘 14 февраля все игры дают счастье ×1.5."]
    return "\n".join(L)


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
                         f"(×{_fmt(mult)}, у всех)")
    _sp_m = SPRING_ALL_HAPPY_MULT
    notes.append(f"🌸 весну: 😊 грустнеет {'быстрее' if _sp_m > 1 else 'медленнее'} "
                 f"(×{_fmt(_sp_m)}, у всех)")
    for season, mods in sorted(SPECIES_SEASON_DECAY_MULT.get(code, {}).items()):
        for stat_key, mult in sorted(mods.items()):
            label = STAT_EMOJI.get({"happy": "happiness"}.get(stat_key, stat_key),
                                   stat_key)
            verdict = f"{label} тратится быстрее" if mult > 1 \
                else f"{label} бережётся"
            notes.append(f"{SEASON_RU[season]} (этот вид): ×{_fmt(mult)} — {verdict}")
    return notes


def tactics_for(code: str, sp: dict) -> list[str]:
    """Персональные советы: что делать именно этому виду."""
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
    if prefers.get("walk", 0) >= 4:
        t.append("🚶 Прогулки — его конёк: чаще запускай, лови события «друг» и")
        t.append("   «хорошо погулял» (+5…+10 😊).")
    if prefers.get("walk", 0) < 0:
        t.append("🚶 На прогулках скучает (−" + str(-prefers["walk"]) + " 😊) — используй")
        t.append("   их ради монет/XP, а счастье набирай иначе.")
    if prefers.get("wash", 0) >= 4:
        t.append("🫧 Чистюля: мытьё даёт ему бонус к 😊 — мой смело, гигиена")
        t.append("   всё равно падает медленно, держи её высокой ради здоровья.")
    if prefers.get("wash", 0) <= -2:
        t.append("⛲ Воды не любит: после мытья −" + str(-prefers["wash"]) + " 😊.")
        t.append(f"   Мой только когда 🫧 угрожает здоровью "
                 f"(<{LOW_STAT_SICK_RISK}), иначе терпи.")
    if prefers.get("train", 0) >= 4:
        t.append("🏋️ Тренировки — его стихия (+6 😊). Качай профильный стат:")
        t.append("   сова — 🧠 интеллекту, щенок — 💪 силу, лиса/шиншилла — 🦋 ловкость.")
    if bonus["sleep_bonus"] > 0:
        t.append(f"😴 Сон — его топливо (+{_fmt(bonus['sleep_bonus'])} ⚡/ч сверх нормы):")
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
        t.append(f"🪙 Добытчик: монеты с прогулок ×{_fmt(bonus['coin_mult'])} —")
        t.append("   зарабатывай на экипировку именно им.")
    if bonus["xp_mult"] > 1.0:
        t.append(f"✨ Учётся быстро: XP ×{_fmt(bonus['xp_mult'])} — расти выше других.")
    price = SPECIES_START_PRICE.get(code)
    if price == 800:
        t.append("🐉 Редкий вид: универсал во всём, но капризен — компенсируй")
        t.append("   быстрым спадом 😊 экипировкой (см. ниже).")
    return t


def gear_tips_for(code: str, sp: dict) -> list[str]:
    """Рекомендации экипировки под слабые места вида."""
    tips: list[str] = []
    if sp["bonus"]["play_happy"] >= 1.1:
        tips.append("🎀 Бантик непоседы / 🩰 Пуанты — усиливают и без того сильные игры (+25%/+20% 😊)")
    if sp["decay"].get("happiness", 1.0) >= 1.2:
        tips.append("Сети 🕊️ Дзен или 🎩 Денди + 🛎️ колокольчик — замедляют падение 😊 (−15%)")
    if sp["bonus"]["play_happy"] < 1.0:
        tips.append("🕊️ Сет «Дзен» — снижает скорость грусти, чтобы реже нужны были игры")
    if sp["decay"].get("hygiene", 1.0) >= 1.2:
        tips.append("🧼 что-то на +гигиену из инвентаря, чтобы реже мыть (мытьё не всем в радость)")
    if sp["prefers"].get("walk", 0) >= 4:
        tips.append("🪙 Всё, что усиливает прогулки/монеты — его главная ферма")
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
    L.append(f"🎾 Игры: ×{_fmt(b['play_happy'])} 😊 · ✨ XP: ×{_fmt(b['xp_mult'])}"
             f" · 🪙 Монеты: ×{_fmt(b['coin_mult'])} · 😴 Сон: +{_fmt(b['sleep_bonus'])} ⚡/ч")
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
        bits.append(f"{lbl} {_fmt(eff)}/ч{mark}")
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
    return "\n".join(L)


def home_text() -> str:
    L = ["📖 <b>Гид по уходу за питомцем</b>", "",
         "Здесь живая справка — она всегда показывает актуальные цифры из",
         "настроек баланса, а не застывший текст. Выбери тему:", ""]
    return "\n".join(L)
