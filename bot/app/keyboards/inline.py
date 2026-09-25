"""Inline-клавиатуры бота. Все экраны — редактирование одного сообщения."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import get_settings


def main_menu(link: str | None = None, reward: int = 0) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🐾 Питомец", callback_data="menu:pet")
    b.button(text="📊 Статы", callback_data="menu:stats")
    b.row()
    b.button(text="🏆 Достижения", callback_data="menu:ach")
    b.button(text="🏅 Топы", callback_data="menu:top")
    b.row()
    b.button(text="🖼 Карточка", callback_data="menu:card")
    b.button(text="🛒 Магазин", callback_data="menu:shop")
    b.row()
    b.button(text="⚙️ Настройки", callback_data="menu:settings")
    settings = get_settings()
    if settings.merch_enabled:
        # Мерч канала — отдельный раздел (не связан с питомцем): категории
        # 👕 Футболки / 🧥 Худи / ☕ Аксессуары внутри бота.
        b.button(text="🧢 Мерч канала", callback_data="menu:merch")
    b.row()
    if link:
        text = "🤝 Пригласить друга" + (f" (+{reward} 🪙)" if reward else "")
        b.button(text=text, url=link)
    b.adjust(2, 2, 2, 2, 1)
    return b.as_markup()


def pet_hub() -> InlineKeyboardMarkup:
    """Хаб тамагочи: [🍎] [🎾] / [💤] [🛁] / [🎒] [🛒] / [🚶] [🏋️] / [🐾] [⬅️]."""
    b = InlineKeyboardBuilder()
    b.button(text="🍎 Покормить", callback_data="pet:feed")
    b.button(text="🎾 Игры", callback_data="pet:games")
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
    b.button(text="🐾 Друзья", callback_data="pet:friends")
    b.button(text="⬅️ Назад", callback_data="menu:main")
    return b.as_markup()


def games_menu() -> InlineKeyboardMarkup:
    """Экран выбора мини-игры (Этап 3.5)."""
    b = InlineKeyboardBuilder()
    b.button(text="🔢 Угадай число", callback_data="game:guess")
    b.button(text="✂️ Камень-ножницы-бумага", callback_data="game:rps")
    b.row()
    b.button(text="⚡ Реакция", callback_data="game:reaction")
    b.button(text="⬅️ К питомцу", callback_data="menu:pet")
    b.adjust(1, 2)
    return b.as_markup()


def rps_keyboard() -> InlineKeyboardMarkup:
    """Ходы для камня-ножниц-бумаги."""
    b = InlineKeyboardBuilder()
    b.button(text="🪨 Камень", callback_data="rps:rock")
    b.button(text="✂️ Ножницы", callback_data="rps:scissors")
    b.button(text="📄 Бумага", callback_data="rps:paper")
    b.row()
    b.button(text="⬅️ К играм", callback_data="pet:games")
    b.adjust(3, 1)
    return b.as_markup()


def reaction_keyboard(start_ts: str) -> InlineKeyboardMarkup:
    """Кнопка «Лови!» с подписанным временем старта (античит)."""
    b = InlineKeyboardBuilder()
    b.button(text="⚡ ЛОВИ!", callback_data=f"react:{start_ts}")
    b.row()
    b.button(text="⬅️ К играм", callback_data="pet:games")
    return b.as_markup()


def guess_hint_keyboard(secret_lo: int, secret_hi: int) -> InlineKeyboardMarkup:
    """Подсказка диапазона для угадайки: быстрые кнопки-варианты."""
    b = InlineKeyboardBuilder()
    mid = (secret_lo + secret_hi) // 2
    for n in (secret_lo, mid, secret_hi):
        b.button(text=str(n), callback_data=f"guess:{n}")
    b.row()
    b.button(text="⬅️ К играм", callback_data="pet:games")
    b.adjust(3, 1)
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


def achievements_list(pairs: list, page: int = 0,
                      total_pages: int | None = None) -> InlineKeyboardMarkup:
    """Постраничная навигация ачивок: ◀ 2/3 ▶ + Назад.

    total_pages можно передать явно (уже посчитан в рендере); если None —
    считаем от размера списка при стандартной странице из 8.
    """
    if total_pages is None:
        total_pages = max(1, (len(pairs) + 8 - 1) // 8)
    b = InlineKeyboardBuilder()
    if page > 0:
        b.button(text="◀️", callback_data=f"ach:page:{page - 1}")
    b.button(text=f"{page + 1}/{total_pages}", callback_data="noop")
    if page < total_pages - 1:
        b.button(text="▶️", callback_data=f"ach:page:{page + 1}")
    b.row()
    b.button(text="⬅️ Назад", callback_data="menu:main")
    return b.as_markup()


def top_tabs(active: str = "week") -> InlineKeyboardMarkup:
    """Переключение периодов топа: день / неделя / всё время."""
    b = InlineKeyboardBuilder()
    for key, label in (("day", "📅 День"), ("week", "🗓 Неделя"), ("all", "♾ Всё")):
        mark = "✅ " if key == active else ""
        b.button(text=f"{mark}{label}", callback_data=f"top:{key}")
    b.row()
    b.button(text="⬅️ Назад", callback_data="menu:main")
    b.adjust(3, 1)
    return b.as_markup()


def settings_keyboard(flags: dict[str, bool]) -> InlineKeyboardMarkup:
    """Экран ⚙️ Настройки: тумблеры уведомлений (Этап 6)."""
    b = InlineKeyboardBuilder()
    labels = {
        "pet_reminders": "🐾 Питомец скучает",
        "streak_reminders": "🔥 Стрик под угрозой",
        "achievement_notifications": "🏆 Достижения",
        "daily_report": "🌅 Ежедневный отчёт",
    }
    for key, label in labels.items():
        on = flags.get(key, True)
        b.button(text=f"{'✅' if on else '❌'} {label}", callback_data=f"set:{key}")
    b.adjust(1)
    b.row()
    b.button(text="⬅️ Назад", callback_data="menu:main")
    return b.as_markup()
