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
    ("☀️ Погода (/weather)", "menu:noop"),
])

def main_menu(link: str | None = None, reward: int = 0,
              page: int = 0, is_admin: bool = False) -> InlineKeyboardMarkup:
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
    kb_rows.append(_page_nav("menu", page, len(pages), title))
    invite_label = f"🤝 Пригласить друга (+{reward})" if reward else "🤝 Пригласить друга"
    if link and settings.show_invite_button:
        kb_rows.append([InlineKeyboardButton(text=invite_label, url=link)])
    kb_rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")])
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
    kb_rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)

def games_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔢 Угадай число · 🧠 помогает", callback_data="game:guess")
    b.row()
    b.button(text="✂️ Камень-ножницы-бумага", callback_data="game:rps")
    b.button(text="🃏 Двадцать одно · 🧠 помогает", callback_data="game:blackjack")
    b.adjust(1, 2)
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()

def rps_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🪨 Камень", callback_data="rps:rock")
    b.button(text="✂️ Ножницы", callback_data="rps:scissors")
    b.button(text="📄 Бумага", callback_data="rps:paper")
    b.adjust(3)
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()

def twentyone_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Ещё карту", callback_data="bj:hit")
    b.button(text="✋ Хватит", callback_data="bj:stand")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()

def guess_hint_keyboard(secret_lo: int, secret_hi: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    mid = (secret_lo + secret_hi) // 2
    for n in (secret_lo, mid, secret_hi):
        b.button(text=str(n), callback_data=f"guess:{n}")
    b.adjust(3)
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()

def back_to_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Меню", callback_data="menu:main")
    return b.as_markup()

def adopt_confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, усыновить", callback_data="pet:adopt_confirm")
    b.button(text="⬅️ Отмена", callback_data="pet:adopt_cancel")
    b.adjust(1)
    return b.as_markup()

def train_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💪 Сила", callback_data="pet:train:strength")
    b.button(text="🏃 Ловкость", callback_data="pet:train:agility")
    b.button(text="🧠 Интеллект", callback_data="pet:train:intellect")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()

def start_pet_name_suggestions(names: list[str]) -> InlineKeyboardMarkup:
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
    from app.services.tamagotchi import SPECIES_DATA
    b = InlineKeyboardBuilder()
    for code, sp in SPECIES_DATA.items():
        b.button(text=f"{sp['emoji']} {sp['title']}", callback_data=f"onb:species:{code}")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="🤝 Пока просто смотреть статистику",
                               callback_data="onb:skip"))
    return b.as_markup()

def adopt_cta_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🥚 Усыновить питомца", callback_data="pet:adopt")
    b.button(text="🏠 Меню", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()

def pet_history_kb(has_current: bool = True) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_current:
        b.button(text="🐾 К текущему", callback_data="menu:pet")
    b.button(text="🥚 Усыновить нового", callback_data="pet:adopt")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()

def achievements_list(pairs: list, page: int = 0,
                      total_pages: int | None = None) -> InlineKeyboardMarkup:
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
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()

def settings_keyboard(flags: dict[str, bool]) -> InlineKeyboardMarkup:
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
    b = InlineKeyboardBuilder()
    if can_fight:
        b.button(text="⚔️ Вызов", callback_data="arena:fight")
    else:
        b.button(text=(hint[:52] or "⏳ Подожди…"), callback_data="arena:noop")
    b.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
    return b.as_markup()

def style_keyboard(svc, pet, slots_page: int = 0,
                   item_page: int = 0) -> InlineKeyboardMarkup:
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
    b.row(InlineKeyboardButton(text="🐾 К питомцу", callback_data="pet:page:1"),
          InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"))
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
