"""Данные о видах питомцев: SPECIES_DATA и справочники.

Вынесено из tamagotchi.py без изменений значений — чтобы UI-модули
(гид по уходу, выбор питомца) не тянули тяжёлый сервис с БД.
tamagotchi.py импортирует отсюда всё обратно (обратная совместимость:
`from app.services.tamagotchi import SPECIES_DATA` продолжает работать).
"""
from __future__ import annotations

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
        "decay": {"hunger": 1.15, "happiness": 1.0, "energy": 1.25, "hygiene": 1.25},
        "bonus": {"play_happy": 1.0, "xp_mult": 1.0, "coin_mult": 1.3, "sleep_bonus": 0.0},
        "prefers": {"walk": +4, "play": +2, "feed": -2, "wash": 0, "train": 0},
        "start": {"strength": 1, "agility": 4, "intellect": 1},
    },
    "chinchilla": {
        "title": "Шиншилла",
        "emoji": "🐭",
        "desc": ("Пушистый чистюля. Почти не пачкается (обожает пыльные ванны) и "
                 "отлично восстанавливается во сне. Но быстро устаёт, не любит игры "
                 "и прогулки, а летом грустнеет от жары."),
        "decay": {"hunger": 1.05, "happiness": 1.0, "energy": 1.2, "hygiene": 0.65},
        "bonus": {"play_happy": 0.9, "xp_mult": 1.0, "coin_mult": 1.0, "sleep_bonus": 2.0},
        "prefers": {"wash": +6, "play": -3, "walk": -2, "feed": +2, "train": 0},
        "start": {"strength": 1, "agility": 4, "intellect": 2},
    },
    "owl": {
        "title": "Совёнок",
        "emoji": "🦉",
        "desc": "Ночной интеллектуал. Быстро учится, отлично восстанавливается во сне, но днём вялый.",
        "decay": {"hunger": 1.05, "happiness": 1.0, "energy": 1.35, "hygiene": 0.95},
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

SPECIES_START_PRICE = {"cat": 0, "dog": 150, "fox": 250, "chinchilla": 300,
                       "owl": 450, "dragon": 800}

SPECIES_BONUS = {k: v["desc"] for k, v in SPECIES_DATA.items()}
