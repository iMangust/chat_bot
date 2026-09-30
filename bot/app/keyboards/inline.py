from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import get_settings

def _two_per_row(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]

def _page_nav(prefix: str, page: int, total: int, title: str) -> list[InlineKeyboardButton]:
    total = max(1, total)
    prev_cb = f"{prefix}:page:{(page - 1) % total}"
    next_cb = f"{prefix}:page:{(page + 1) % total}"
    label = f"{title} 📖 {page + 1}/{total}" if total > 1 else title
    return [
        InlineKeyboardButton(text="◀️", callback_data=prev_cb),
        InlineKeyboardButton(text=label, callback_data=f"{prefix}:noop"),
        InlineKeyboardButton(text="▶️", callback_data=next_cb),
    ]

MENU_PAGES: list[tuple[str, list[tuple[str, str]]]] = [
    ("🎮 Игра", [
        ("🐾 Питомец", "menu:pet"),
        ("🧢 Наш мерч", "menu:merch"),
        ("📅 Мероприятия", "menu:events"),
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

ADMIN_TOOLS_PAGE = ("🛠 Инструменты админа", [
    ("🧢 Управление мерчем", "madmin:home"),
    ("📅 Управление мероприятиями", "evadmin:home"),
    ("⚙️ Мои уведомления", "menu:settings"),
    ("☀️ Погода (/weather)", "menu:weather"),
])

def main_menu(link: str | None = None, reward: int = 0,
              page: int = 0, is_admin: bool = False,
              has_pet: bool = True) -> InlineKeyboardMarkup:
    settings = get_settings()
    pages = list(MENU_PAGES)
    if is_admin:
        pages.append(ADMIN_TOOLS_PAGE)
    page %= len(pages)
    title, actions = pages[page]
    buttons = [InlineKeyboardButton(text=t, callback_data=cb)
               for t, cb in actions
               if not (cb == "menu:merch" and not settings.merch_enabled)]
    kb_rows: list[list[InlineKeyboardButton]] = _two_per_row(buttons)
    # У пользователя без питомца в разделе «Игра» нет центрального экрана —
    # добавляем явный CTA прямо в меню, чтобы не уводить его стрелками
    # листания в онбординг против воли. Отдельной строкой ПОСЛЕ постраничной
    # навигации (◀️ 📖 ▶️): так «▶️» гарантированно остаётся последней кнопкой
    # своей строки и ведёт на следующую страницу меню, а не открывает выбор
    # питомца (CTA никогда не «склеивается» со стрелками в один ряд).
    kb_rows.append(_page_nav("menu", page, len(pages), title))
    if not has_pet:
        kb_rows.append([InlineKeyboardButton(
            text="🥚 Усыновить питомца", callback_data="pet:adopt")])
    invite_label = f"🤝 Пригласить друга (+{reward})" if reward else "🤝 Пригласить друга"
    if link and settings.show_invite_button:
        kb_rows.append([InlineKeyboardButton(text=invite_label, url=link)])
    # Это и есть главное меню — отдельная кнопка «Домой» здесь не нужна.
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)

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

WALK_BLOCKED_CB = {"pet:wash", "pet:sleep", "pet:train"}

def pet_hub(page: int = 0, critical: bool = False,
            sleeping: bool = False, walking: bool = False) -> InlineKeyboardMarkup:
    n = len(PET_PAGES)
    page %= n
    title, actions = PET_PAGES[page]
    if sleeping and page == 0:
        actions = [(("⏰ Разбудить", "pet:wake") if lbl == "💤 Спать" else (lbl, cb))
                   for lbl, cb in actions]
    if walking:
        actions = [(("🏠 Вернуть с прогулки", "pet:end_walk")
                    if cb == "pet:walk" else (lbl, cb))
                   for lbl, cb in actions
                   if cb not in WALK_BLOCKED_CB]
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
    # Хаб питомца — верхний уровень раздела: только «🏠 Меню».
    kb_rows.append([InlineKeyboardButton(text=HOME_LABEL, callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)


# Коллбэки «своего раздела»: если запись стека — экран того же раздела,
# где сейчас находится пользователь (вкладки хаба питомца, листание
# пагинации, повторный вход), это не «выход наружу». Для кнопки «Назад»
# такие записи пропускаем и копаемся глубже в истории — иначе «Назад»
# вёл бы на тот же экран, где пользователь уже сидит (самопетля).
# Особый случай — 'menu:*': это входы в РАЗНЫЕ разделы (мерч, события,
# статы…), поэтому для профилных секций ('stats', 'merch', …) они всегда
# валидные точки возврата; исключение — 'menu:pet', которое для секций
# внутри хаба питомца (games/shop/style/…) является «своим» входом.
_SECTION_OWN_PREFIXES: dict[str, tuple[str, ...]] = {
    "games": ("game", "rps", "guess", "bj", "pet"),
    "arena": ("arena", "pet"),
    "friends": ("fr", "pet"),
    "shop": ("shop", "buy", "use", "inv", "style", "pet"),
    "inv": ("inv", "use", "shop", "buy", "style", "pet"),
    "style": ("style", "pet"),
    "pet": ("pet",),
    "merch": ("merch",),
    "events": ("ev",),
    "stats": ("ach", "top"),   # соседние экраны «Профиля» — не точка возврата
    "ach": ("ach",),           # own head + 'menu:ach' (см. функцию ниже)
    "top": ("top",),           # own head + 'menu:top'
    "card": (),                # карточка — тупик: возврат по истории/в меню
    "settings": ("set",),      # own head + 'menu:settings'
}

def _is_own_section_entry(cb: str, section: str | None) -> bool:
    if cb in ("menu:main", "menu:home"):
        return False  # главное меню — валидная точка возврата с любого экрана
    if cb == "menu:weather":
        return True   # кнопка-команда /weather: возвращаться на неё бессмысленно
    sec = section or ""
    # Выход из профиля в его же корневой экран (stats → ach/top) — петля.
    if sec and cb == f"menu:{sec}":
        return True
    head = cb.split(":")[0]
    if head in _SECTION_OWN_PREFIXES.get(sec, ()):
        return True
    # 'menu:*' — входы в разделы главного меню. Внутри подраздела хаба
    # питомца повторный 'menu:pet' — это тот же хаб (листание/повторный
    # вход), а не выход наружу; для прочих секций 'menu:...' остаётся
    # валидной исторической точкой возврата.
    if head == "menu" and cb == "menu:pet" and sec in {"games", "arena",
                                                       "friends", "shop",
                                                       "inv", "style", "pet"}:
        return True
    return False


def _nav_back_cb(section: str | None, chat_id: int | None) -> str | None:
    """Callback для кнопки «⬅️ Назад»: ближайшая подходящая запись стека
    навигации (экран, откуда пришли на этот), а если стек пуст или вся
    история — «свой» раздел — безопасный корень раздела. Синхронная версия
    читает локальное зеркало стека; актуальность обеспечивает
    NavStackMiddleware, который после каждого коллбэка перечитывает
    Redis-стек в зеркало (см. middlewares/nav_stack.py)."""
    from app.utils import nav as _nav
    stack = _nav.mem_stack(chat_id)
    root = SECTION_ROOTS.get(section or "", "menu:main")
    if not stack:
        return root
    for cb in reversed(stack):
        if not cb or cb.endswith(":noop") or cb == "noop":
            continue
        # Корень текущего раздела в истории — это сам текущий экран или
        # его листание: не «Назад», а повторный вход. Пропускаем, чтобы
        # кнопка не вела на то же место, где пользователь уже сидит.
        if root != "menu:main" and cb == root:
            continue
        if _is_own_section_entry(cb, section):
            continue
        # Если корень раздела лежит в истории ГЛУБЖЕ найденной записи,
        # «Назад» должен вести в корень раздела, а не перескакивать его
        # сразу в главное меню (Мерч → Категория: «Назад» = список мерча).
        if root != "menu:main" and root in stack[:stack.index(cb)]:
            return root
        return cb
    return root


def with_nav(b: InlineKeyboardBuilder, section: str | None,
             chat_id: int | None = None) -> InlineKeyboardBuilder:
    """Добавляет в билдер строку «⬅️ Назад» + «🏠 Меню»; «Назад» учитывает
    локальную историю переходов этого чата."""
    return append_nav(b, section, _nav_back_cb(section, chat_id))

def games_menu(chat_id: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔢 Угадай число · 🧠 помогает", callback_data="game:guess")
    b.row()
    b.button(text="✂️ Камень-ножницы-бумага", callback_data="game:rps")
    b.button(text="🃏 Двадцать одно · 🧠 помогает", callback_data="game:blackjack")
    b.adjust(1, 2)
    with_nav(b, "games", chat_id)
    return b.as_markup()

def rps_keyboard(chat_id: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🪨 Камень", callback_data="rps:rock")
    b.button(text="✂️ Ножницы", callback_data="rps:scissors")
    b.button(text="📄 Бумага", callback_data="rps:paper")
    b.adjust(3)
    # «Назад» — по истории (обычно в меню игр): игрок может передумать.
    append_nav(b, "games", back_cb=_nav_back_cb("games", chat_id))
    return b.as_markup()

def twentyone_keyboard(chat_id: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Ещё карту", callback_data="bj:hit")
    b.button(text="✋ Хватит", callback_data="bj:stand")
    b.adjust(2)
    append_nav(b, "games", back_cb=_nav_back_cb("games", chat_id))
    return b.as_markup()

def guess_hint_keyboard(secret_lo: int, secret_hi: int,
                        chat_id: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    mid = (secret_lo + secret_hi) // 2
    for n in (secret_lo, mid, secret_hi):
        b.button(text=str(n), callback_data=f"guess:{n}")
    b.adjust(3)
    append_nav(b, "games", back_cb=_nav_back_cb("games", chat_id))
    return b.as_markup()

def back_to_main() -> InlineKeyboardMarkup:
    """Экранный хаб (например, страница питомца): только «🏠 Меню».
    На экранах помельче используется nav_row/append_nav с кнопкой «⬅️ Назад»."""
    b = InlineKeyboardBuilder()
    b.button(text=HOME_LABEL, callback_data="menu:main")
    return b.as_markup()


# ============================================================================
# Единая навигация «Назад / Меню» по всем разделам бота.
#
# Правила:
#  • «⬅️ Назад» ведёт туда, откуда пользователь пришёл на экран. Источник
#    перехода лежит в стеке навигации (app/utils/nav.py); если стек пуст
#    (рестарт без Redis, первое сообщение), кнопка деградирует до безопасного
#    корня раздела из таблицы ниже — никогда не в чужой раздел;
#  • «🏠 Меню» всегда ведёт в главное меню и сбрасывает стек;
#  • корни разделов — обычные «menu:*»/«pet:page:*» коллбэки, которые
#    обрабатываются штатными хендлерами («menu:card», «menu:stats», «menu:ach»,
#    «menu:top» продублированы мостами в роутере events, который регистрируется
#    раньше social/stats).
#
# Значения по умолчанию для back_cb (когда стек пуст):
SECTION_ROOTS: dict[str, str] = {
    "pet": "menu:pet",            # 🐾 Питомец: уход / вещи / досуг
    "shop": "pet:page:1",         # 🛒 Магазин питомца ← вкладка «🎒 Вещи»
    "inv": "pet:page:1",          # 🎒 Инвентарь ← вкладка «🎒 Вещи»
    "style": "pet:page:1",        # 🎨 Гардероб ← вкладка «🎒 Вещи»
    "games": "pet:page:2",        # 🎮 Мини-игры ← вкладка «🎮 Досуг»
    "arena": "pet:page:2",        # 🏟 Арена ← вкладка «🎮 Досуг»
    "friends": "pet:page:2",      # 🐾 Друзья ← вкладка «🎮 Досуг»
    "merch": "menu:merch",        # 🧢 Наш мерч
    "events": "menu:events",      # 📅 Мероприятия
    "stats": "menu:stats",        # 📊 Статистика
    "ach": "menu:ach",            # 🏆 Достижения
    "top": "menu:top",            # 🏅 Топы
    "card": "menu:card",          # 🖼 Карточка профиля
    "settings": "menu:settings",  # ⚙️ Уведомления
}

BACK_LABEL = "⬅️ Назад"
HOME_LABEL = "🏠 Меню"


def _nav_buttons(back_cb: str | None) -> list[InlineKeyboardButton]:
    out: list[InlineKeyboardButton] = []
    if back_cb and back_cb != "menu:main":
        out.append(InlineKeyboardButton(text=BACK_LABEL, callback_data=back_cb))
    out.append(InlineKeyboardButton(text=HOME_LABEL, callback_data="menu:main"))
    return out


def onb_back_from_name(chat_id: int | None) -> list[InlineKeyboardButton]:
    """Строка навигации для шага «Имя питомца» (шаг 2 из 3).

    «⬅️ Назад» ведёт строго на шаг 1 (выбор вида): общий сборщик навигации
    здесь не подходит — под шагом имени в стеке лежит 'pet:adopt', и
    generic-логика показала бы кнопку «Назад → pet:adopt», которая просто
    перерисовывает текущий экран (визуальная самопетля). «🏠 Меню» —
    гарантированный выход из онбординга со сбросом стека."""
    return [InlineKeyboardButton(text=BACK_LABEL,
                                 callback_data="onb:species_back"),
            InlineKeyboardButton(text=HOME_LABEL, callback_data="menu:main")]


def nav_row(section: str | None, back_cb: str | None = None
            ) -> list[InlineKeyboardButton]:
    """Готовая нижняя строка навигации для ручной сборки клавиатуры.
    back_cb=None — взять корень раздела (используется, когда стек уже учтён
    или недоступен); back_cb="" — подавить кнопку «Назад» (только «Меню»)."""
    if back_cb is None:
        back_cb = SECTION_ROOTS.get(section or "", "menu:main")
    return _nav_buttons(back_cb)


def append_nav(b: InlineKeyboardBuilder, section: str | None,
               back_cb: str | None = None) -> InlineKeyboardBuilder:
    """Добавляет в билдер нижнюю строку «⬅️ Назад» + «🏠 Меню»."""
    b.row(*nav_row(section, back_cb))
    return b


def nav_kb(section: str | None,
           rows: list[list[InlineKeyboardButton]] | None = None,
           back_cb: str | None = None) -> InlineKeyboardMarkup:
    """Клавиатура из строк экрана + нижняя навигационная строка."""
    b = InlineKeyboardBuilder()
    for row in (rows or []):
        if row:
            b.row(*row)
    append_nav(b, section, back_cb)
    return b.as_markup()

def adopt_confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, усыновить", callback_data="pet:adopt_confirm")
    b.button(text="⬅️ Отмена", callback_data="pet:adopt_cancel")
    b.adjust(1)
    return b.as_markup()

def train_menu(chat_id: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💪 Сила", callback_data="pet:train:strength")
    b.button(text="🏃 Ловкость", callback_data="pet:train:agility")
    b.button(text="🧠 Интеллект", callback_data="pet:train:intellect")
    b.adjust(2)
    with_nav(b, "pet", chat_id)
    return b.as_markup()

def start_pet_name_suggestions(names: list[str],
                               chat_id: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for n in names:
        b.button(text=f"✨ {n}", callback_data=f"onb:name:{n}")
    b.adjust(2)
    # Шаг 2 из 3: «⬅️ Назад» — на шаг 1 (выбор вида), «🏠 Меню» — выход в
    # главное меню со сбросом стека. Раньше здесь не было никакой возможности
    # выйти: кнопка «Назад» вела обратно на тот же экран (самопетля).
    b.row(*onb_back_from_name(chat_id))
    return b.as_markup()

def onboard_done() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Открыть меню", callback_data="menu:main")
    return b.as_markup()

def welcome_start_button() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Начать", callback_data="onb:start")
    return b.as_markup()

def onboard_back_cb(chat_id: int | None) -> str:
    """Callback кнопки «⬅️ Назад» на экране выбора вида питомца (шаг 1).

    Возвращает туда, откуда пользователь вошёл в онбординг. Исключения —
    записи, которые перерисовали бы тот же экран (визуальные самопетли):
    'onb:*' (шаги игнорируются стеком как шум), 'pet:adopt*' (повторно
    откроют пикер), 'menu:main'/'menu:home' (под окном онбординга и так
    лежит главное меню). Если подходящей записи нет — «Назад» не нужен,
    остаётся только «🏠 Меню» (пустая строка = подавить кнопку)."""
    from app.utils import nav as _nav
    for cb in reversed(_nav.mem_stack(chat_id)):
        if not cb or cb.endswith(":noop") or cb == "noop":
            continue
        head = cb.split(":")[0]
        if head in ("onb", "pet") or cb in ("menu:main", "menu:home"):
            continue
        return cb
    return ""


def species_picker(chat_id: int | None = None,
                   back_cb: str | None = None) -> InlineKeyboardMarkup:
    from app.services.tamagotchi import SPECIES_DATA
    b = InlineKeyboardBuilder()
    for code, sp in SPECIES_DATA.items():
        b.button(text=f"{sp['emoji']} {sp['title']}", callback_data=f"onb:species:{code}")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="🤝 Пока просто смотреть статистику",
                               callback_data="onb:skip"))
    # Выход из онбординга: «⬅️ Назад» — туда, откуда вошли (обычно главное
    # меню), «🏠 Меню» — гарантированный выход в меню со сбросом стека.
    # Пользователь не должен оказываться в ловушке из шагов выбора.
    # back_cb="" — явно подавить «Назад» (экран перерисован самим «Назад»).
    if back_cb is None:
        back_cb = onboard_back_cb(chat_id)
    append_nav(b, None, back_cb=back_cb or "")
    return b.as_markup()

def adopt_cta_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🥚 Усыновить питомца", callback_data="pet:adopt")
    b.button(text="🏠 Меню", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()

def pet_history_kb(has_current: bool = True,
                   chat_id: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_current:
        b.button(text="🐾 К текущему", callback_data="menu:pet")
    b.button(text="🥚 Усыновить нового", callback_data="pet:adopt")
    b.adjust(2)
    with_nav(b, "pet", chat_id)
    return b.as_markup()

def achievements_list(pairs: list, page: int = 0,
                      total_pages: int | None = None,
                      chat_id: int | None = None) -> InlineKeyboardMarkup:
    if total_pages is None:
        total_pages = max(1, (len(pairs) + 8 - 1) // 8)
    total_pages = max(1, total_pages)
    b = InlineKeyboardBuilder()
    b.row(*_page_nav("ach", page, total_pages, "🏆 Достижения"))
    with_nav(b, "ach", chat_id)
    return b.as_markup()

TOP_SECTION_LABELS = {"talk": "💬 Болтуны", "react": "💖 Реакции",
                      "streak": "🔥 Серии", "pets": "🐾 Питомцы",
                      "levels": "⭐ Уровни",
                      "overall": "👑 Общий", "emotional": "🎭 Эмоциональные",
                      "karma": "💚 Добряки"}

def top_tabs(active: str = "week", section: str = "talk",
             chat_id: int | None = None) -> InlineKeyboardMarkup:
    from app.handlers.stats import TOP_SECTIONS
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
    with_nav(b, "top", chat_id)
    return b.as_markup()

def settings_keyboard(flags: dict[str, bool],
                      chat_id: int | None = None) -> InlineKeyboardMarkup:
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
    with_nav(b, "settings", chat_id)
    return b.as_markup()

def arena_keyboard(can_fight: bool = True, hint: str = "",
                   chat_id: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if can_fight:
        b.button(text="⚔️ Вызов", callback_data="arena:fight")
    else:
        b.button(text=(hint[:52] or "⏳ Подожди…"), callback_data="arena:noop")
    with_nav(b, "arena", chat_id)
    return b.as_markup()

def style_keyboard(svc, pet, slots_page: int = 0,
                   item_page: int = 0,
                   chat_id: int | None = None) -> InlineKeyboardMarkup:
    from app.services.tamagotchi import STYLE_ITEMS_PER_PAGE

    b = InlineKeyboardBuilder()
    color_key, _worn = svc.customization(pet)
    for key, info in svc.PET_COLORS.items():
        on = (key == "default" and not color_key) or key == color_key
        price = info["price"]
        label = ("✅ " if on else "") + info["title"] + ("" if price == 0 else f" · {price}🪙")
        b.button(text=label, callback_data=f"style:color:{key}")
    b.adjust(2)

    slot_keys = list(svc.GEAR_SLOTS.keys())
    gear = svc.gear_map(pet)
    for k in slot_keys:
        cur = gear.get(k)
        title = svc.GEAR_SLOTS[k]
        label = title + (f" {cur}" if cur else " —")
        b.button(text=label, callback_data=f"style:slot:{k}:{item_page}")
    b.adjust(3)

    if slots_page is not None and 0 <= slots_page < len(slot_keys):
        slot = slot_keys[slots_page]
        owned = set((pet.settings_extra or {}).get("owned") or [])
        catalog = [(e, it) for e, it in svc.PET_ACCESSORIES.items() if it["slot"] == slot]
        pages = max(1, -(-len(catalog) // STYLE_ITEMS_PER_PAGE))
        item_page = max(0, min(item_page, pages - 1))
        chunk = catalog[item_page * STYLE_ITEMS_PER_PAGE:(item_page + 1) * STYLE_ITEMS_PER_PAGE]
        for emoji, it in chunk:
            if gear.get(slot) == emoji:
                label = f"✅ {emoji} Снять"
            elif emoji in owned:
                label = f"🎒 {emoji} Надеть"
            else:
                label = f"{emoji} {it['title']} · {it['price']}🪙"
            b.button(text=label, callback_data=f"style:wear:{slot}:{emoji}:{slots_page}:{item_page}")
        b.adjust(1)
        if pages > 1:
            nav = []
            if item_page > 0:
                nav.append(InlineKeyboardButton(
                    text="⬅️", callback_data=f"style:page:{slots_page}:{item_page - 1}"))
            nav.append(InlineKeyboardButton(
                text=f"{item_page + 1}/{pages}", callback_data="style:noop"))
            if item_page < pages - 1:
                nav.append(InlineKeyboardButton(
                    text="➡️", callback_data=f"style:page:{slots_page}:{item_page + 1}"))
            b.row(*nav)
    # «Назад» — по истории (обычно вкладка «🎒 Вещи» хаба питомца).
    with_nav(b, "style", chat_id)
    return b.as_markup()

def open_slot_of(cb_data: str) -> int | None:
    parts = cb_data.split(":")
    try:
        if parts[1] == "slot":
            from app.services.tamagotchi import TamagotchiService
            return list(TamagotchiService.GEAR_SLOTS.keys()).index(parts[2])
        if parts[1] == "wear":
            return int(parts[-2])
        if parts[1] == "page":
            return int(parts[2])
    except (IndexError, ValueError):
        pass
    return None
