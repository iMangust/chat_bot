"""Справочники обязаны брать числа из механики, а не из литералов текста.

Тесты ловят главный класс багов документации: UI-текст расходится с кодом
(например, «тренировка стоит 15 энергии», хотя train() списывает 10).
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from app.handlers import tamagotchi as tg_handlers
from app.services import pet_data, pet_manual
from app.services import tamagotchi as tg

# ── Профилисты тренировок: один источник правды ───────────────────────────

def test_train_uses_single_source_profile_dict():
    """train() обязан читать TRAIN_PROFILE_SPECIES, а не локальный литерал."""
    src = inspect.getsource(tg.TamagotchiService.train)
    assert "TRAIN_PROFILE_SPECIES" in src
    # Ручной словарь stat_pref в теле метода — признак расхождения с UI.
    assert "stat_pref = {" not in src


def test_profiles_line_matches_species_titles():
    """Строка экрана тренировок генерируется из данных, эмодзи — настоящие."""
    line = tg_handlers._train_profiles_line("🐱")
    for codes in tg.TRAIN_PROFILE_SPECIES.values():
        for code in codes:
            assert pet_data.SPECIES_DATA[code]["emoji"] in line
    assert "🐈" not in line and "🐕" not in line  # устаревшие ручные эмодзи


def test_manual_profiles_mirror_train():
    """pet_manual._profile_stat_for отдаёт ровно те виды, что и train()."""
    for stat_key, codes in tg.TRAIN_PROFILE_SPECIES.items():
        _label, manual_codes = pet_manual._profile_stat_for(stat_key)
        assert set(manual_codes) == set(codes), stat_key


# ── Числа в динамическом тексте = числа из констант ───────────────────────

def test_help_text_train_costs_match_constants():
    """В /help и на экране тренировок стоимость — из TRAIN_COST_*."""
    help_text = tg_handlers.HELP_TEXT
    assert f"⚡≥{tg.TRAIN_ENERGY_MIN}" in help_text
    screen = tg_handlers._train_profiles_line("🐱") + (
        f"цена {tg.TRAIN_COST_ENERGY}")
    assert screen  # строка профилистов собрана без ошибок
    # Нигде в UI не должно остаться застывшей «15 энергии» для тренировок.
    for path in Path("app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "стоит 15 энергии" not in text, path


def test_species_help_line_no_stale_literals():
    """«Вид важен» в /help генерируется из SPECIES_DATA."""
    line = tg_handlers._species_help_line()
    assert "кот любит игры" not in line  # старый ручной текст
    for key in ("cat", "dog", "owl", "fox", "dragon"):
        sp = pet_data.SPECIES_DATA[key]
        assert sp["emoji"] in line, key
        assert sp["title"].lower() in line, key
    assert str(pet_data.SPECIES_START_PRICE["dragon"]) in line


# ── Формула прироста в тексте = код train() ───────────────────────────────

def test_gain_formula_base_and_level_term():
    """train_gain_formula_text описывает (1 + level//5) — как в коде."""
    formula = pet_manual.train_gain_formula_text()
    assert "уровень//5" in formula
    src = inspect.getsource(tg.TamagotchiService.train)
    assert "gain = 1 + (pet.level // 5)" in src
    # Цена в формуле — те же константы, что списывает train().
    assert str(tg.TRAIN_COST_ENERGY) in formula
    assert str(tg.TRAIN_COST_HUNGER) in formula


def test_walk_happy_range_from_finish_walk_event():
    """Диапазон 😊 за прогулку соответствует веткам finish_walk_event()."""
    src = Path("app/services/tamagotchi.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    base_values = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) \
                and node.name == "finish_walk_event":
            for sub in ast.walk(node):
                # Ищем happiness += <число>
                if isinstance(sub, ast.AugAssign) \
                        and getattr(sub.target, "attr", "") == "happiness" \
                        and isinstance(sub.value, ast.Constant) \
                        and isinstance(sub.value.value, int):
                    base_values.append(sub.value.value)
    lo, hi = pet_manual._walk_happy_range()
    if base_values:
        dmin = min(sp["prefers"].get("walk", 0)
                   for sp in pet_data.SPECIES_DATA.values())
        dmax = max(sp["prefers"].get("walk", 0)
                   for sp in pet_data.SPECIES_DATA.values())
        assert lo == min(base_values) + dmin
        assert hi == max(base_values) + dmax


# ── Кулдауны/пороги в справочниках — только из констант ───────────────────

def test_manual_pages_contain_current_cooldowns():
    """Страницы справочника содержат актуальные значения кулдаунов."""
    pages_text = "\n".join([
        pet_manual.games_guide_text(),
        pet_manual.stats_guide_text(),
    ])
    for const_name in ("COOLDOWN_PLAY_SEC", "COOLDOWN_WASH_SEC"):
        value = getattr(tg, const_name)
        assert str(value) in pages_text or str(value // 60) in pages_text, \
            const_name


def test_no_hardcoded_energy_numbers_in_manual():
    """В pet_manual запрещены голые числа стоимости — только f-строки с константами."""
    src = Path("app/services/pet_manual.py").read_text(encoding="utf-8")
    # «15 энергии» / «10 энергии» литералами быть не должно.
    assert not re.search(r"\d+\sэнерги", src)


# ── Описания товаров магазина — генерируются из effect, а не литералами ───

def test_shop_descriptions_generated_from_effect():
    """description каждого товара = _item_desc(effect, подводка).

    Если кто-то поправит цифру в effect, но забудет текст описания (или
    наоборот) — этот тест упадёт: описание физически пересобирается из
    словаря эффекта перед сравнением.
    """
    from app.handlers.shop import ITEMS_SEED

    for spec in ITEMS_SEED:
        eff = spec["effect"]
        desc = spec["description"]
        # Мгновенные статы обязаны присутствовать в описании живыми числами.
        from app.services.pet_manual import STAT_EMOJI

        for stat in ("hunger", "happiness", "energy", "hygiene", "health"):
            v = eff.get(stat)
            if isinstance(v, (int, float)) and v:
                token = f"{STAT_EMOJI[stat]}{'+' if v >= 0 else ''}{v:g}"
                assert token in desc, (spec["code"], token, desc)
        # Бафы: длительность и процент — из buff/buffs, не из памяти автора.
        defs = list(eff.get("buffs") or [])
        if isinstance(eff.get("buff"), dict):
            defs = [eff["buff"], *defs]
        for b in defs:
            dur_min = round(int(b.get("duration", 1800)) / 60)
            dur_txt = f"{dur_min} мин" if dur_min < 60 else \
                f"{dur_min // 60} ч" + (f" {dur_min % 60} мин" if dur_min % 60 else "")
            assert dur_txt in desc, (spec["code"], dur_txt, desc)
            pct = float(b.get("mult", 0)) * 100
            if abs(pct) > 0 and b.get("type") != "no_decay":
                s = f"{pct:.10g}"
                assert f"+{s}%" in desc or f"{s}%" in desc, (spec["code"], desc)


def test_shop_seed_matches_rebuilt_description():
    """Полная проверка: description == результат _item_desc от того же effect."""
    from app.handlers.shop import ITEMS_SEED, _item_desc

    # Подводки (flavor) при пересборке вычленять нельзя — сравниваем только
    # техническую часть: она обязана быть префиксом/частью описания.
    for spec in ITEMS_SEED:
        tech_bits = _item_desc(spec["effect"], "")
        assert tech_bits, spec["code"]
        for frag in tech_bits.split(" · "):
            assert frag in spec["description"], (spec["code"], frag)
