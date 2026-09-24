"""Inline-клавиатуры бота. Все экраны — редактирование одного сообщения."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🐾 Питомец", callback_data="menu:pet")
    b.button(text="📊 Статы", callback_data="menu:stats")
    b.row()
    b.button(text="🏆 Достижения", callback_data="menu:ach")
    b.button(text="🏅 Топы", callback_data="menu:top")
    b.row()
    b.button(text="⚙️ Настройки", callback_data="menu:settings")
    b.adjust(2, 2, 1)
    return b.as_markup()


def pet_hub() -> InlineKeyboardMarkup:
    """Хаб тамагочи: [🍎] [🎾] / [💤] [🛁] / [🎒] [🛒] / [🚶] [📊] / [🏆] [⬅️]."""
    b = InlineKeyboardBuilder()
    b.button(text="🍎 Покормить", callback_data="pet:feed")
    b.button(text="🎾 Играть", callback_data="pet:play")
    b.row()
    b.button(text="💤 Спать", callback_data="pet:sleep")
    b.button(text="🛁 Помыть", callback_data="pet:wash")
    b.row()
    b.button(text="🎒 Инвентарь", callback_data="pet:inv")
    b.button(text="🛒 Магазин", callback_data="pet:shop")
    b.row()
    b.button(text="🚶 Прогулка", callback_data="pet:walk")
    b.button(text="🏋️ Тренировки", callback_data="pet:train")
    b.row()
    b.button(text="⬅️ Назад", callback_data="menu:main")
    return b.as_markup()


def back_to_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="menu:main")
    return b.as_markup()


def train_menu() -> InlineKeyboardMarkup:
    """Экран тренировок: выбор характеристики."""
    b = InlineKeyboardBuilder()
    b.button(text="💪 Сила", callback_data="pet:train:strength")
    b.button(text="🏃 Ловкость", callback_data="pet:train:agility")
    b.button(text="🧠 Интеллект", callback_data="pet:train:intellect")
    b.row()
    b.button(text="⬅️ К питомцу", callback_data="menu:pet")
    b.adjust(3, 1)
    return b.as_markup()


def start_pet_name_suggestions(names: list[str]) -> InlineKeyboardMarkup:
    """Кнопки с вариантами имени питомца (онбординг)."""
    b = InlineKeyboardBuilder()
    for n in names:
        b.button(text=f"✨ {n}", callback_data=f"onb:name:{n}")
    b.adjust(2)
    return b.as_markup()


def onboard_done() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚀 В главное меню", callback_data="menu:main")
    return b.as_markup()


def welcome_start_button() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Начать", callback_data="onb:start")
    return b.as_markup()


def species_picker() -> InlineKeyboardMarkup:
    """Кнопки выбора вида питомца в онбординге."""
    from app.services.tamagotchi import SPECIES_DATA
    b = InlineKeyboardBuilder()
    for code, sp in SPECIES_DATA.items():
        b.button(text=f"{sp['emoji']} {sp['title']}", callback_data=f"onb:species:{code}")
    b.adjust(2)
    return b.as_markup()


def achievements_list(pairs: list[tuple[int, bool]], page: int = 0,
                      page_size: int = 8) -> InlineKeyboardMarkup:
    """Простая постраничная навигация ачивок: ◀ 1/3 ▶ + Назад."""
    total_pages = max(1, (len(pairs) + page_size - 1) // page_size)
    b = InlineKeyboardBuilder()
    if page > 0:
        b.button(text="◀️", callback_data=f"ach:page:{page - 1}")
    b.button(text=f"{page + 1}/{total_pages}", callback_data="noop")
    if page < total_pages - 1:
        b.button(text="▶️", callback_data=f"ach:page:{page + 1}")
    b.row()
    b.button(text="⬅️ Назад", callback_data="menu:main")
    return b.as_markup()
