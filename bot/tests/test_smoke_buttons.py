"""Smoke-тесты кнопок: каждый callback из клавиатур имеет обработчик.

Покрывает «мёртвые кнопки» (например, pet:noop в навигации хаба питомца)
и логику главного меню: магазин — только в хабе питомца, мерч — отдельная
кнопка, скрытая до включения merch_enabled; вместо «Реакции» — игра «21».
"""
from __future__ import annotations

import re

import pytest

from _helpers import _btns

from app.config import get_settings
from app.handlers import (arena, games, merch, settings, shop, social,
                          start, stats, tamagotchi)
from app.keyboards import inline as ikb

# все модули-роутеры с @router.callback_query(F.data ...)
_HANDLER_MODULES = [arena, games, merch, settings, shop, social, tamagotchi, start, stats]


def _handled_patterns() -> list[re.Pattern]:
    """Собирает все условия F.data из декораторов callback_query."""
    pats: list[re.Pattern] = []
    for mod in _HANDLER_MODULES:
        src = open(mod.__file__, encoding="utf-8").read()
        for m in re.finditer(
            r'F\.data\s*==\s*"([^"]+)"|'
            r'F\.data\.startswith\(\s*"([^"]+)"|'
            r'F\.data\.in_\(\{([^}]*)\}',
            src,
        ):
            if m.group(1):
                pats.append(re.compile(rf"^{re.escape(m.group(1))}$"))
            elif m.group(2):
                pats.append(re.compile(rf"^{re.escape(m.group(2))}"))
            else:
                for lit in re.findall(r'"([^"]+)"', m.group(3)):
                    pats.append(re.compile(rf"^{re.escape(lit)}$"))
    return pats


def _all_button_callbacks() -> set[str]:
    """Все callback_data всех кнопок всех экранов (url-кнопки не берём)."""
    kbs = [
        ikb.main_menu(link="https://t.me/x", reward=50, page=0),
        ikb.main_menu(link=None, reward=0, page=1),
        ikb.pet_hub(page=0),
        ikb.pet_hub(page=1),
        ikb.pet_hub(page=2),
        ikb.pet_hub(page=0, sleeping=True),
        ikb.pet_hub(page=0, critical=True),
        ikb.games_menu(),
        ikb.rps_keyboard(),
        ikb.twentyone_keyboard(),
        ikb.guess_hint_keyboard(1, 50),
        ikb.back_to_main(),
        ikb.train_menu(),
        ikb.start_pet_name_suggestions(["Тест"]),
        ikb.onboard_done(),
        ikb.welcome_start_button(),
        ikb.species_picker(),
        ikb.adopt_cta_kb(),
        ikb.pet_history_kb(True),
        ikb.pet_history_kb(False),
        ikb.top_tabs(),
        ikb.settings_keyboard({"pet_reminders": True}),
        ikb.arena_keyboard(),
        ikb.arena_keyboard(can_fight=False, hint="⏳ Отдыхает"),
        ikb.style_keyboard({"default": ("Обычный", 0)}, {"🎩": ("Цилиндр", 10)},
                           "default", []),
    ]
    out: set[str] = set()
    for kb in kbs:
        for b in _btns(kb):
            if b.callback_data:
                out.add(b.callback_data)
    return out


class TestButtonWiring:
    def test_every_button_has_handler(self):
        """Ни одной «мёртвой» кнопки: клик по любой должен обрабатываться."""
        pats = _handled_patterns()
        unhandled = {cb for cb in _all_button_callbacks()
                     if not any(p.match(cb) for p in pats)}
        assert not unhandled, f"нет обработчиков для: {sorted(unhandled)}"

    def test_shop_not_in_main_menu(self):
        """Магазин принадлежит питомцу: в главном меню его нет."""
        for page in range(ikb.menu_page_count()):
            kb = ikb.main_menu(page=page)
            texts = [b.text for b in _btns(kb)]
            cbs = [b.callback_data or "" for b in _btns(kb)]
            assert not any("Магазин" in t for t in texts)
            assert "menu:shop" not in cbs and "pet:shop" not in cbs

    def test_shop_only_in_pet_hub(self):
        kb = ikb.pet_hub(page=1)  # страница «🎒 Вещи»
        assert any((b.callback_data or "") == "pet:shop" for b in _btns(kb))

    def test_merch_is_separate_and_hidden_until_ready(self):
        """Мерч — отдельная кнопка вне питомца, по умолчанию скрыта."""
        assert get_settings().merch_enabled is False
        cbs = [b.callback_data or "" for b in _btns(ikb.main_menu(page=0))]
        assert "menu:merch" not in cbs
        for page in range(ikb.pet_page_count()):
            hub_cbs = [b.callback_data or "" for b in _btns(ikb.pet_hub(page=page))]
            assert not any(c.startswith("merch:") for c in hub_cbs)

    def test_merch_shown_when_enabled(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "merch_enabled", True)
        cbs = [b.callback_data or "" for b in _btns(ikb.main_menu(page=0))]
        assert "menu:merch" in cbs  # включается одним флагом, код готов

    def test_sleep_toggle_button(self):
        awake = {(b.callback_data or "") for b in _btns(ikb.pet_hub(page=0))}
        asleep = {(b.callback_data or "") for b in _btns(ikb.pet_hub(page=0, sleeping=True))}
        assert "pet:sleep" in awake and "pet:wake" not in awake
        assert "pet:wake" in asleep and "pet:sleep" not in asleep

    def test_no_reaction_game(self):
        """Старая «Реакция» удалена полностью, вместо неё — «21»."""
        all_cbs = _all_button_callbacks()
        # игра «Реакция» удалена: никаких game:reaction / react: кнопок;
        # top:*react* — это вкладка топа реакций, к игре отношения не имеет
        assert not any(c.startswith("game:reaction") or c.startswith("react:")
                       for c in all_cbs)
        assert "game:blackjack" in all_cbs
        texts = [b.text for b in _btns(ikb.games_menu())]
        assert any("Двадцать одно" in t for t in texts)
        assert not any("Реакц" in t for t in texts)
