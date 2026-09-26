"""Инлайн-клавиатуры бота.

Единая стилистика навигации (v1.5.3):
* контент — по 2 кнопки в ряд, максимум 3 ряда на страницу;
* 4-й ряд — ◀️ · «Название 📖 i/n» · ▶️ (перехлёст зациклен);
* выход с экрана — ОДНА кнопка «🏠 Меню» внизу (без дублей «⬅️ Назад»).
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import get_settings


def _two_per_row(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    """Раскладывает кнопки по две в ряд (непарная — одна)."""
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def _page_nav(prefix: str, page: int, total: int, title: str) -> list[InlineKeyboardButton]:
    """Ряд навигации: ◀️ · «Название 📖 i/n» · ▶️, зацикливание страниц."""
    total = max(1, total)
    prev_cb = f"{prefix}:page:{(page - 1) % total}"
    next_cb = f"{prefix}:page:{(page + 1) % total}"
    label = f"{title} 📖 {page + 1}/{total}" if total > 1 else title
    return [
        InlineKeyboardButton(text="◀️", callback_data=prev_cb),
        InlineKeyboardButton(text=label, callback_data=f"{prefix}:noop"),
        InlineKeyboardButton(text="▶️", callback_data=next_cb),
    ]


# ─── 🏠 Главное меню ────────────────────────────────────────────────────────

MENU_PAGES: list[tuple[str, list[tuple[str, str]]]] = [
    ("🎮 Игра", [
        ("🐾 Питомец", "menu:pet"),
        ("🛒 Магазин", "menu:shop"),
        ("🧢 Мерч канала", "menu:merch"),
    ]),
    ("👤 Профиль", [
        ("📊 Статистика", "menu:stats"),
        ("🏆 Награды", "menu:ach"),
        ("🏅 Топы", "menu:top"),
        ("🖼 Карточка", "menu:card"),
        ("⚙️ Уведомления", "menu:settings"),
    ]),
]


def menu_page_count() -> int:
    return len(MENU_PAGES)


def main_menu(link: str | None = None, reward: int = 0,
              page: int = 0) -> InlineKeyboardMarkup:
    """Пагинированное главное меню: 2 кнопки в ряд, ◀️ i/n ▶️, один выход 🏠."""
    settings = get_settings()
    page %= len(MENU_PAGES)
    title, actions = MENU_PAGES[page]
    buttons = [InlineKeyboardButton(text=t, callback_data=cb)
               for t, cb in actions
               if not (cb == "menu:merch" and not settings.merch_enabled)]
    kb_rows: list[list[InlineKeyboardButton]] = _two_per_row(buttons)
    kb_rows.append(_page_nav("menu", page, len(MENU_PAGES), title))
    invite_label = f"🤝 Пригласить друга (+{reward})" if reward else "🤝 Пригласить друга"
    if link:
        kb_rows.append([InlineKeyboardButton(text=invite_label, url=link)])
    kb_rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)


# ─── 🐾 Хаб питомца ─────────────────────────────────────────────────────────

PET_PAGES: list[tuple[str, list[tuple[str, str]]]] = [
    ("🧴 Уход", [
        ("🍎 Покормить", "pet:feed"),
        ("🛁 Помыть", "pet:wash"),
        ("💤 Спать", "pet:sleep"),
        ("🏋️ Тренировки", "pet:train"),
    ]),
    ("🎒 Вещи", [
        ("🎒 Инвентарь", "pet:inv"),
        ("🛒 Магазин", "pet:shop"),
        ("🎨 Стиль", "pet:style"),
    ]),
    ("🎮 Досуг", [
        ("🎾 Игры", "pet:games"),
        ("🚶 Прогулка", "pet:walk"),
        ("🐾 Друзья", "pet:friends"),
        ("🏟 Арена", "arena:open"),
    ]),
]


def pet_page_count() -> int:
    return len(PET_PAGES)


def pet_hub(page: int = 0, critical: bool = False) -> InlineKeyboardMarkup:
    """Постраничный хаб питомца (2 в ряд, ◀️ i/n ▶️, один выход 🏠).

    critical=True: на странице «Уход» вместо обычных действий — реанимация
    и усыновление нового. «📜 История» есть на каждой странице.
    """
    n = len(PET_PAGES)
    page %= n
    title, actions = PET_PAGES[page]
    if critical and page == 0:
        buttons = [
            InlineKeyboardButton(text="💖 Реанимация", callback_data="pet:revive"),
            InlineKeyboardButton(text="🥚 Усыновить нового", callback_data="pet:adopt"),
        ]
    else:
        buttons = [InlineKeyboardButton(text=t, callback_data=cb) for t, cb in actions]
    buttons.append(InlineKeyboardButton(text="📜 История питомцев", callback_data="pet:history"))
    kb_rows = _two_per_row(buttons)
    kb_rows.append(_page_nav("pet", page, n, title))
    kb_rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)


# ─── 🕹 Мини-игры ────────────────────────────────────────────────────────────

def games_menu() -> InlineKeyboardMarkup:
    """Экран выбора мини-игры. Подписи объясняют механику."""
    b = InlineKeyboardBuilder()
    b.button(text="🔢 Угадай число · 🧠 помогает", callback_data="game:guess")
    b.row()
    b.button(text="✂️ Камень-ножницы-бумага", callback_data="game:rps")
    b.button(text="⚡ Реакция · 🏃 помогает", callback_data="game:reaction")
    b.adjust(1, 2)
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()


def rps_keyboard() -> InlineKeyboardMarkup:
    """Ходы для камня-ножницы-бумаги."""
    b = InlineKeyboardBuilder()
    b.button(text="🪨 Камень", callback_data="rps:rock")
    b.button(text="✂️ Ножницы", callback_data="rps:scissors")
    b.button(text="📄 Бумага", callback_data="rps:paper")
    b.adjust(3)
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()


def reaction_keyboard(start_ts: str) -> InlineKeyboardMarkup:
    """Кнопка «Лови!» с подписанным временем старта (античит)."""
    b = InlineKeyboardBuilder()
    b.button(text="⚡ ЛОВИ!", callback_data=f"react:{start_ts}")
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()


def guess_hint_keyboard(secret_lo: int, secret_hi: int) -> InlineKeyboardMarkup:
    """Подсказка диапазона для угадайки: быстрые кнопки-варианты."""
    b = InlineKeyboardBuilder()
    mid = (secret_lo + secret_hi) // 2
    for n in (secret_lo, mid, secret_hi):
        b.button(text=str(n), callback_data=f"guess:{n}")
    b.adjust(3)
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()


def back_to_main() -> InlineKeyboardMarkup:
    """Одиночная кнопка выхода «🏠 Меню» для промежуточных экранов."""
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Меню", callback_data="menu:main")
    return b.as_markup()


# ─── 🏋️ Тренировки ──────────────────────────────────────────────────────────

def train_menu() -> InlineKeyboardMarkup:
    """Экран тренировок: выбор характеристики."""
    b = InlineKeyboardBuilder()
    b.button(text="💪 Сила", callback_data="pet:train:strength")
    b.button(text="🏃 Ловкость", callback_data="pet:train:agility")
    b.button(text="🧠 Интеллект", callback_data="pet:train:intellect")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()


# ─── 🚀 Онбординг / выбор вида ──────────────────────────────────────────────

def start_pet_name_suggestions(names: list[str]) -> InlineKeyboardMarkup:
    """Кнопки с вариантами имени питомца (онбординг)."""
    b = InlineKeyboardBuilder()
    for n in names:
        b.button(text=f"✨ {n}", callback_data=f"onb:name:{n}")
    b.adjust(2)
    return b.as_markup()


def onboard_done() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Открыть меню", callback_data="menu:main")
    return b.as_markup()


def welcome_start_button() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Начать", callback_data="onb:start")
    return b.as_markup()


def species_picker() -> InlineKeyboardMarkup:
    """Выбор вида питомца + «просто смотреть»: питомец — опция, не обязанность."""
    from app.services.tamagotchi import SPECIES_DATA
    b = InlineKeyboardBuilder()
    for code, sp in SPECIES_DATA.items():
        b.button(text=f"{sp['emoji']} {sp['title']}", callback_data=f"onb:species:{code}")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="🤝 Пока просто смотреть статистику",
                               callback_data="onb:skip"))
    return b.as_markup()


def adopt_cta_kb() -> InlineKeyboardMarkup:
    """Экран «питомца нет»: завести или вернуться в меню."""
    b = InlineKeyboardBuilder()
    b.button(text="🥚 Усыновить питомца", callback_data="pet:adopt")
    b.button(text="🏠 Меню", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


def pet_history_kb(has_current: bool = True) -> InlineKeyboardMarkup:
    """Экран истории питомцев."""
    b = InlineKeyboardBuilder()
    if has_current:
        b.button(text="🐾 К текущему", callback_data="menu:pet")
    b.button(text="🥚 Усыновить нового", callback_data="pet:adopt")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()


# ─── 🏆 Достижения / топы / настройки / арена ───────────────────────────────

def achievements_list(pairs: list, page: int = 0,
                      total_pages: int | None = None) -> InlineKeyboardMarkup:
    """Постраничная навигация ачивок: ◀️ · «🏆 Достижения 📖 i/n» · ▶️."""
    if total_pages is None:
        total_pages = max(1, (len(pairs) + 8 - 1) // 8)
    total_pages = max(1, total_pages)
    b = InlineKeyboardBuilder()
    b.row(*_page_nav("ach", page, total_pages, "🏆 Достижения"))
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()


TOP_SECTION_LABELS = {"talk": "💬 Болтуны", "react": "💖 Реакции",
                      "streak": "🔥 Серии", "pets": "🐾 Питомцы",
                      "levels": "⭐ Уровни",
                      "overall": "👑 Общий", "emotional": "🎭 Эмоциональные",
                      "karma": "💚 Добряки"}


def top_tabs(active: str = "week", section: str = "talk") -> InlineKeyboardMarkup:
    """Топы: сверху вкладки периода, ниже ◀️ раздел · i/n · ▶️, один выход 🏠."""
    from app.handlers.stats import TOP_SECTIONS  # локальный импорт: без цикла
    keys = [k for k, _ in TOP_SECTIONS]
    idx = keys.index(section) if section in keys else 0
    n = len(keys)
    period_keys = ["day", "week", "all"]
    b = InlineKeyboardBuilder()
    for key in period_keys:
        label = {"day": "📅 День", "week": "🗓 Неделя", "all": "♾ Всё время"}[key]
        mark = "✅ " if key == active else ""
        b.button(text=f"{mark}{label}", callback_data=f"top:{key}:{keys[idx]}")
    b.adjust(3)
    prev_cb = f"top:{active}:{keys[(idx - 1) % n]}"
    next_cb = f"top:{active}:{keys[(idx + 1) % n]}"
    title = TOP_SECTION_LABELS.get(keys[idx], "🏅 Топы")
    b.row(
        InlineKeyboardButton(text="◀️", callback_data=prev_cb),
        InlineKeyboardButton(text=f"{title} 📖 {idx + 1}/{n}", callback_data="top:noop"),
        InlineKeyboardButton(text="▶️", callback_data=next_cb),
    )
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()


def settings_keyboard(flags: dict[str, bool]) -> InlineKeyboardMarkup:
    """Экран ⚙️ Настройки: тумблеры уведомлений (по 2 в ряд)."""
    labels = {
        "pet_reminders": "🐾 Питомец скучает",
        "streak_reminders": "🔥 Стрик под угрозой",
        "achievement_notifications": "🏆 Достижения",
        "daily_report": "🌅 Ежедневный отчёт",
    }
    b = InlineKeyboardBuilder()
    for key, label in labels.items():
        on = flags.get(key, True)
        b.button(text=f"{'✅' if on else '❌'} {label}", callback_data=f"set:{key}")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()


def arena_keyboard(can_fight: bool = True, hint: str = "") -> InlineKeyboardMarkup:
    """Арена: кнопка боя (или некликабельная подсказка кулдауна) + выход 🏠.

    В aiogram 3 «disabled» — это объект DisabledButton, а не bool; вместо
    серой кнопки показываем некликабельную подсказку (arena:noop).
    """
    b = InlineKeyboardBuilder()
    if can_fight:
        b.button(text="⚔️ Вызов", callback_data="arena:fight")
    else:
        b.button(text=(hint[:52] or "⏳ Подожди…"), callback_data="arena:noop")
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
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
    for emoji, (title, price) in accessories.items():
        label = ("✅ " if emoji in worn else "") + f"{emoji} {title} · {price}🪙"
        b.button(text=label, callback_data=f"style:acc:{emoji}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()
