"""Inline-клавиатуры бота. Все экраны — редактирование одного сообщения."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import get_settings


def main_menu(link: str | None = None, reward: int = 0) -> InlineKeyboardMarkup:
    """Главное меню. Подписи кнопок самодокументируемы (UX v1.4.7):
    коротко поясняем, что внутри, чтобы не было «котов в мешке»."""
    b = InlineKeyboardBuilder()
    b.button(text="🐾 Питомец · уход и игры", callback_data="menu:pet")
    b.row()
    b.button(text="📊 Мои статистика", callback_data="menu:stats")
    b.button(text="🏆 Мои награды", callback_data="menu:ach")
    b.row()
    b.button(text="🏅 Топы чата", callback_data="menu:top")
    b.button(text="🖼 Карточка профиля", callback_data="menu:card")
    b.row()
    b.button(text="🛒 Магазин · за монеты", callback_data="menu:shop")
    b.button(text="⚙️ Уведомления · язык", callback_data="menu:settings")
    settings = get_settings()
    if settings.merch_enabled:
        # Мерч канала — отдельный раздел (не связан с питомцем): категории
        # 👕 Футболки / 🧥 Худи / ☕ Аксессуары внутри бота.
        b.row()
        b.button(text="🧢 Мерч канала", callback_data="menu:merch")
    if link:
        b.row()
        text = "🤝 Пригласить друга" + (f" (+{reward} 🪙)" if reward else "")
        b.button(text=text, url=link)
    return b.as_markup()


# ---------------------------------------------------------------------------
# Пагинированный хаб тамагочи (UX v1.4.7)
# Раньше на одном экране было 13 кнопок — легко запутаться. Теперь действия
# разбиты по страницам «уход → вещи → досуг», навигация ◀️/▶️ внизу.
# ---------------------------------------------------------------------------

PET_PAGES: list[tuple[str, list[tuple[str, str]]]] = [
    ("🧴 Уход", [("🍎 Покормить", "pet:feed"), ("🛁 Помыть", "pet:wash"),
                 ("💤 Спать", "pet:sleep"), ("🏋️ Тренировки", "pet:train")]),
    ("🎒 Вещи", [("🎒 Инвентарь", "pet:inv"), ("🛒 Магазин", "pet:shop"),
                 ("🎨 Стиль", "pet:style")]),
    ("🎮 Досуг", [("🎾 Игры", "pet:games"), ("🚶 Прогулка", "pet:walk"),
                  ("🐾 Друзья", "pet:friends"), ("🏟 Арена", "arena:open")]),
]


def pet_page_count() -> int:
    return len(PET_PAGES)


def pet_hub(page: int = 0, critical: bool = False) -> InlineKeyboardMarkup:
    """Хаб тамагочи с постраничной навигацией (2 кнопки в ряд).

    Страница 0 — «Уход», 1 — «Вещи», 2 — «Досуг». Внизу: ◀️ · 📖 1/3 · ▶️
    и ⬅️ Назад в главное меню. Перехлест страницы зацикливается.

    critical=True (v1.4.7): на странице «Уход» вместо обычных действий —
    «💖 Реанимация» и «🥚 Усыновить нового» + «📜 История питомцев».
    """
    n = len(PET_PAGES)
    page %= n
    title, actions = PET_PAGES[page]
    b = InlineKeyboardBuilder()
    if critical and page == 0:
        b.button(text="💖 Реанимация", callback_data="pet:revive")
        b.button(text="🥚 Усыновить нового", callback_data="pet:adopt")
    else:
        for text, cb_data in actions:
            b.button(text=text, callback_data=cb_data)
    b.row()
    b.button(text="📜 История питомцев", callback_data="pet:history")
    b.adjust(2)
    b.row()
    b.button(text="◀️", callback_data=f"pet:page:{(page - 1) % n}")
    b.button(text=f"📖 {title} · {page + 1}/{n}", callback_data="noop")
    b.button(text="▶️", callback_data=f"pet:page:{(page + 1) % n}")
    b.row()
    b.button(text="⬅️ Назад", callback_data="menu:main")
    b.adjust(3, 1)
    return b.as_markup()


def games_menu() -> InlineKeyboardMarkup:
    """Экран выбора мини-игры (Этап 3.5). Подписи объясняют механику."""
    b = InlineKeyboardBuilder()
    b.button(text="🔢 Угадай число · 🧠 помогает", callback_data="game:guess")
    b.row()
    b.button(text="✂️ Камень-ножницы-бумага", callback_data="game:rps")
    b.button(text="⚡ Реакция · 🏃 помогает", callback_data="game:reaction")
    b.row()
    b.button(text="⬅️ К питомцу", callback_data="menu:pet")
    b.adjust(1, 2, 1)
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
    """Кнопки выбора вида питомца в онбординге + «просто смотреть» (v1.4.7):
    питомец — опция, а не обязанность; без него доступны топы и статистика."""
    from app.services.tamagotchi import SPECIES_DATA
    b = InlineKeyboardBuilder()
    for code, sp in SPECIES_DATA.items():
        b.button(text=f"{sp['emoji']} {sp['title']}", callback_data=f"onb:species:{code}")
    b.adjust(2)
    b.row()
    b.button(text="🤝 Пока просто смотреть статистику", callback_data="onb:skip")
    return b.as_markup()


def adopt_cta_kb() -> InlineKeyboardMarkup:
    """Экран «питомца нет» (v1.4.7): завести или вернуться в меню."""
    b = InlineKeyboardBuilder()
    b.button(text="🥚 Усыновить питомца", callback_data="pet:adopt")
    b.button(text="⬅️ В меню", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


def pet_history_kb(has_current: bool = True) -> InlineKeyboardMarkup:
    """Клавиатура экрана истории питомцев (v1.4.7)."""
    b = InlineKeyboardBuilder()
    if has_current:
        b.button(text="🐾 К текущему", callback_data="menu:pet")
    b.button(text="🥚 Усыновить нового", callback_data="pet:adopt")
    b.row()
    b.button(text="⬅️ Назад", callback_data="menu:main")
    b.adjust(2, 1)
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


TOP_SECTION_LABELS = {"talk": "💬 Болтуны", "react": "💖 Реакции",
                      "streak": "🔥 Серии", "pets": "🐾 Питомцы",
                      "levels": "⭐ Уровни",
                      # v1.4.7 — новые номинации общего топа:
                      "overall": "👑 Общий", "emotional": "🎭 Эмоциональные",
                      "karma": "💚 Добряки"}


def top_tabs(active: str = "week", section: str = "talk") -> InlineKeyboardMarkup:
    """Навигация топов (UX v1.4.7): сверху — период, ниже — ◀️ раздел ▶️.

    Раньше пять топов сваливались в одно простыню-сообщение; теперь каждый
    раздел отдельная страница: «💬 Болтуны 2/5» с листанием и вкладками периода.
    """
    from app.handlers.stats import TOP_SECTIONS  # локальный импорт: без цикла
    keys = [k for k, _ in TOP_SECTIONS]
    idx = keys.index(section) if section in keys else 0
    n = len(keys)
    b = InlineKeyboardBuilder()
    for key, label in (("day", "📅 День"), ("week", "🗓 Неделя"), ("all", "♾ Всё время")):
        mark = "✅ " if key == active else ""
        b.button(text=f"{mark}{label}", callback_data=f"top:{key}:{keys[idx]}")
    b.row()
    b.button(text="◀️", callback_data=f"top:{active}:{keys[(idx - 1) % n]}")
    b.button(text=f"{TOP_SECTION_LABELS.get(keys[idx], '🏅')} {idx + 1}/{n}",
             callback_data="noop")
    b.button(text="▶️", callback_data=f"top:{active}:{keys[(idx + 1) % n]}")
    b.row()
    b.button(text="⬅️ Назад", callback_data="menu:main")
    b.adjust(3, 3, 1)
    return b.as_markup()


def settings_keyboard(flags: dict[str, bool], lang: str = "ru") -> InlineKeyboardMarkup:
    """Экран ⚙️ Настройки: тумблеры уведомлений + язык (i18n)."""
    b = InlineKeyboardBuilder()
    labels = {
        "pet_reminders": "🐾 Напомнить покормить",
        "streak_reminders": "🔥 Стрик под угрозой",
        "achievement_notifications": "🏆 Достижения",
        "daily_report": "🌅 Ежедневный отчёт",
    }
    for key, label in labels.items():
        on = flags.get(key, True)
        b.button(text=f"{'✅' if on else '❌'} {label}", callback_data=f"set:{key}")
    b.adjust(1)
    b.row()
    ru = "✅" if lang == "ru" else "🌐"
    en = "✅" if lang == "en" else "🌐"
    b.button(text=f"{ru} Русский  |  {en} English", callback_data="lang:toggle")
    b.row()
    b.button(text="⬅️ Назад", callback_data="menu:main")
    return b.as_markup()


def arena_keyboard(can_fight: bool = True, hint: str = "") -> InlineKeyboardMarkup:
    """Арена питомцев: кнопка вызова (неактивна при кулдауне/лимите) + топ.

    В aiogram 3 «disabled» — это объект DisabledButton, а не bool; в старых
    версиях флага нет вовсе, поэтому вместо серой кнопки показываем некликабельную
    подсказку (callback без обработчика = мёртвая кнопка).
    """
    b = InlineKeyboardBuilder()
    if can_fight:
        b.button(text="⚔️ Вызов", callback_data="arena:fight")
    else:
        b.button(text=(hint[:52] or "⏳ Подожди…"), callback_data="arena:noop")
    b.row()
    b.button(text="⬅️ К питомцу", callback_data="menu:pet")
    b.adjust(1)
    return b.as_markup()


def style_keyboard(colors: dict[str, tuple[str, int]],
                   accessories: dict[str, tuple[str, int]],
                   current_color: str | None,
                   worn: list[str]) -> InlineKeyboardMarkup:
    """Гардероб питомца: окрасы по 2 в ряд, аксессуары списком."""
    b = InlineKeyboardBuilder()
    for key, (title, price) in colors.items():
        on = (key == "default" and not current_color) or key == current_color
        label = ("✅ " if on else "") + title + ("" if price == 0 else f" · {price}🪙")
        b.button(text=label, callback_data=f"style:color:{key}")
    b.adjust(2)
    b.row()
    for emoji, (title, price) in accessories.items():
        label = ("✅ " if emoji in worn else "") + f"{emoji} {title} · {price}🪙"
        b.button(text=label, callback_data=f"style:acc:{emoji}")
    b.adjust(1)
    b.row()
    b.button(text="⬅️ К питомцу", callback_data="menu:pet")
    return b.as_markup()
