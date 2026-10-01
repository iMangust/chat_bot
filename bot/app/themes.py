"""Темы оформления бота: «Стандарт» и «Готика».

Каждая тема описывает, как выглядят кнопки главного меню/хаба питомца,
тексты приветствия и главного меню, а также какие эмодзи подмешиваются в
эффекты (реакции на сообщения). Выбор темы хранится в ``User.settings_extra``
под ключом ``theme`` (JSON-колонка уже есть — миграция БД не требуется).

Механика:
  • текущая тема берётся из контекстных переменных aiogram, которые ставит
    ``ThemeMiddleware`` (app/middlewares/theme.py);
  • текстовые шаблоны тем — это полные строки со стандартными плейсхолдерами
    ({name}, {channel_name}, {channel_line});
  • клавиатуры и эффекты заменяют эмодзи через таблицу соответствия
    (стандартный эмодзи -> готический), поэтому новые экраны подхватывают
    тему автоматически.
"""
from __future__ import annotations

import contextvars
import fnmatch
from dataclasses import dataclass, field
from typing import Any

CURRENT_THEME: contextvars.ContextVar[str] = contextvars.ContextVar(
    "bot_theme", default="standard")


def set_theme(name: str | None) -> None:
    CURRENT_THEME.set(THEMES.get(name or "", STANDARD).key)


def current_theme_key() -> str:
    return CURRENT_THEME.get()


@dataclass(frozen=True)
class Theme:
    key: str
    title: str            # название темы для экрана настроек
    tagline: str          # короткое описание темы
    preview_emoji: tuple[str, ...]  # превью-эмодзи темы
    # callback_data кнопок, текст которых нужно заменить (glob-паттерны)
    label_cb_patterns: tuple[str, ...] = ()
    label_overrides: dict[str, str] = field(default_factory=dict)
    # глобальная замена эмодзи в текстах/подписях кнопок
    emoji_map: dict[str, str] = field(default_factory=dict)
    # замены в строках i18n (ключ -> новый текст целиком)
    string_overrides: dict[str, str] = field(default_factory=dict)
    welcome_text: str | None = None
    main_menu_text: str | None = None
    settings_intro: str | None = None


STANDARD = Theme(key="standard", title="☀️ Стандарт",
                 tagline="Привычный вид бота: яркое солнце и тёплые эмодзи.",
                 preview_emoji=("🐾", "✨"))

GOTHIC = Theme(
    key="gothic",
    title="🦇 Готика",
    tagline="Полночь, руины и вороны: весь мир бота одет в траурный бархат.",
    preview_emoji=("🦇", "🕸"),
    label_cb_patterns=(
        "menu:*", "pet:*", "arena:*", "style:*", "set:*", "game*",
        "rps:*", "guess:*", "bj:*", "shop:*", "inv:*", "buy:*", "use:*",
    ),
    label_overrides={
        "menu:page:*": "🃏 Страница",
        "menu:noop": "🕯 Монастырь",
        "menu:main": "🕯 Монастырь",
        "menu:home": "🕯 Монастырь",
        "menu:pet": "🐈‍⬛ Кошка-демон",
        "menu:merch": "⚰ Наш склеп",
        "menu:events": "🌑 Ночные службы",
        "menu:stats": "📜 Летопись",
        "menu:ach": "💀 Реликвии",
        "menu:top": "🥀 Пантеон",
        "menu:card": "🖼 Портрет",
        "menu:settings": "🗝 Настройки",
        "menu:weather": "🌧 Погода",
        "pet:feed": "🍷 Угощение",
        "pet:wash": "🕯 Омовение",
        "pet:sleep": "🌑 Отход ко сну",
        "pet:wake": "🕯 Пробуждение",
        "pet:train": "⚔ Испытания",
        "pet:inv": "🎒 Сундук",
        "pet:shop": "🕸 Лавка травницы",
        "pet:style": "🖤 Облачение",
        "pet:games": "🃏 Гадания",
        "pet:walk": "🌲 Тропа в лесу",
        "pet:end_walk": "🏚 Вернуть из леса",
        "pet:friends": "🕸 Союзы",
        "pet:history": "📜 Хроника ушедших",
        "pet:adopt": "🖤 Приютить тень",
        "pet:revive": "🕯 Восстать из пепла",
        "arena:open": "🏟 Яма",
        "arena:fight": "⚔ Помра",
        "set:pet_reminders": "🐈‍⬛ Кошка тоскует",
        "set:streak_reminders": "🕯 Свеча угасает",
        "set:achievement_notifications": "💀 Реликвии",
        "set:daily_report": "🌒 Вечерний дневник",
    },
    emoji_map={
        # животные и персонажи
        "🐾": "🕸", "🐱": "🐈‍⬛", "🐈": "🐈‍⬛", "🐶": "🐕‍🦺", "🦊": "🦉",
        "🐹": "🐀", "🦉": "🦇", "🐉": "🐍", "🥚": "🜏",
        # еда / уход
        "🍎": "🍷", "🍏": "🥀", "😋": "🍷", "🛁": "🕯", "🫧": "🌫",
        "💊": "🧪", "❤️‍🩹": "🖤", "💖": "🕯", "❤️": "🖤",
        # сон / время
        "💤": "🌑", "🌙": "🕯", "☀️": "🌒", "🥱": "😪", "⏰": "🕰",
        "🌅": "🌒", "🌄": "🌒",
        # игры / победы
        "🎮": "🃏", "🥳": "😈", "🎉": "🖤", "🤩": "👁", "🏆": "💀",
        "🏅": "🥀", "🎁": "⚰", "✨": "🕸", "🌟": "🕯", "💫": "🌘",
        "⭐": "✴", "🎊": "🥀", "🥇": "💀", "🎯": "🩸",
        # эмоции
        "😀": "😈", "🙂": "🌚", "😊": "😏", "😿": "🥀", "😩": "😵‍💫",
        "😴": "🌑", "🚨": "☠", "🔥": "🕯", "💬": "🗨", "👑": "⛧",
        # деньги / магазин
        "🪙": "🩸", "💰": "⛓", "🛒": "🕸", "🧢": "⚰", "🎒": "📿",
        "🛡": "⛓", "🛡️": "⛓", "🧹": "🕯",
        # статистика / профиль
        "📊": "📜", "🖼": "🖼", "👤": "👤", "🤝": "🫱", "📢": "🔔",
        "🚶": "🚶", "🏃": "🏃", "🏋️": "⚔", "🏋": "⚔", "💪": "⚔",
        "🧠": "👁", "🌳": "🌲", "🌦️": "🌧", "🌦": "🌧", "🌧️": "🌧",
        "🏠": "🏚", "⬅️": "⬅", "✅": "✔", "❌": "✖", "📌": "🗡",
        "🧴": "🕯", "🎨": "🖤", "🔮": "🔮", "⚗": "⚗",
    },
    string_overrides={
        "pet.eaten": "🍷 Пригубила кровь… Ням-ням{tail}",
        "pet.sleeping_deny": "🌑 Она спит в склебе — не тревожь покой!",
        "pet.too_tired_play": "😵‍💫 Тень слишком измотана для игр. Пусть поспит!",
        "pet.won_game": "🖤 Победа! Кошка в восторге! +{xp} XP",
        "pet.lost_game": "🌚 Не повезло, но тени всё равно весело. +{xp} XP",
        "pet.already_sleeping": "🌑 Она уже спит.",
        "pet.fell_asleep": "🌑 Тень уснула до {time} (камчатское время). Энергия будет восстанавливаться во сне.",
        "pet.not_sleeping": "🐈‍⬛ Она и не спит.",
        "pet.woken": "🕰 Ты разбудил питомца! Проспал {hours} ч → накоплено ⚡ +{energy} энергии (уже учтено на карточке).",
        "pet.sleeping_deny_feed": "🌑 Во сне не кормят! Сначала разбуди (кнопка «🕯 Пробудить»).",
        "pet.sleeping_deny_train": "🌑 Спящая тень не тренируется — разбуди её сначала.",
        "pet.sleeping_deny_heal": "🌑 Зелье во сне не дают — разбуди питомца и исцели.",
        "pet.washed": "🕯 Омовение совершено! Гигиена +40",
        "pet.healed": "🧪 Зелье помогло! Здоровье +35",
        "pet.not_sick": "😈 Питомец здоров, зелье не нужно.",
        "pet.walk_started": "🌲 Тень ушла в ночной лес на {hours} ч. Вернётся в {time} — с новостями!",
        "pet.walk_already": "🌲 Тень уже бродит по лесу… Вернётся в {time} (через {minutes}).",
        "pet.not_walking": "🏚 Она и не гуляет — дома, в склебе.",
        "pet.walk_returned_early": "🌲 Ты позвал тень домой! Бродила {hours} ч → {reward} (уже учтено на карточке).",
        "pet.walk_back_line": "🌲 Сейчас в лесу… Вернётся в {time} (через {minutes}).",
        "pet.walk_deny_sleep": "🌲 {name} бродит — спать на кладбище нельзя! Дождись возвращения.",
        "pet.walk_deny_wash": "🕯 {name} бродит — омовение на кладбище негде совершить! Дождись возвращения.",
        "pet.walk_deny_train": "⚔ {name} бродит — тренироваться на кладбище негде! Дождись возвращения.",
        "pet.walk_deny_medicine": "🧪 {name} бродит — зелья на прогулке не дают! Дождись возвращения.",
        "pet.train_done": "⚔ Испытание завершено! {label} +{gain}",
        "pet.train_stat": "⚔ Сила", "pet.train_agi": "🌑 Ловкость", "pet.train_int": "👁 Интеллект",
        "pet.cooldown_feed": "🕰 Тень только что пила! Подожди {sec} сек.",
        "pet.cooldown_generic": "🕰 Перерыв между действиями: {sec} сек.",
        "pet.critical_deny": "☠ {name} при смерти! Обычный уход уже не поможет — "
                             "нужна «🕯 Восстание из пепла» на странице «Уход» (200 🩸).",
        "pet.critical_banner": "☠ <b>Смертный час!</b> Здоровье и один из показателей на нуле. "
                               "Спасай: «🕯 Восстание из пепла» за 200 🩸 или приюти новую тень.",
        "pet.no_more_lives": "🥀 У {name} больше не осталось жизней — восстание недоступно. "
                             "Можно только приютить новую тень (старая уйдёт в хронику).",
        "pet.revive_done": "🕯 {name} восстала из пепла! За это снято ✴ {stars}.",
        "pet.revive_no_money": "⛓ Не хватает крови: нужно {need}, у тебя {have}. "
                               "Зарабатывай активностью в чате!",
        "pet.not_critical": "✔ Тень в порядке — обряд не нужен.",
        "pet.history_empty": "📜 Хроника пуста: это твоя первая тень!",
        "pet.history_title": "📜 <b>Хроника ушедших теней</b>",
        "pet.mood_great": "Великолепно!",
        "pet.mood_good": "Тёмное довольство",
        "pet.mood_ok": "Нормально",
        "pet.mood_sad": "Тоскует… удели внимание",
        "pet.mood_sick": "При смерти! Нужно зелье 🧪",
        "pet.mood_sleeping": "Спит в склебе… не буди 🌑",
        "pet.mood_hungry": "Жаждет! Дай выпить 🍷",
        "top.title": "🥀 <b>Пантеон чата · {period}</b>",
        "top.talkers": "🗨 <b>Шептуны</b>",
        "top.reactors": "🖤 <b>По полученным знакам</b>",
        "top.streaks": "🕯 <b>Горевшие свечи</b>",
        "top.pets": "🕸 <b>Тени</b>",
        "top.levels": "💀 <b>Ступени посвящённых</b>",
        "top.empty": "   пока пусто — будь первым проклятым! 🗨",
        "top.me_hint": " 👈 <i>это ты</i>",
        "top.award_hint": "<i>/award — итоги прошлой недели с дарами ⚰</i>",
        "stats.title": "📜 <b>Твоя летопись</b>",
        "ach.title": "💀 <b>Реликвии</b>",
        "shop.title": "🕸 <b>Лавка травницы</b> · у тебя 🩸 {coins}",
        "shop.bought": "Куплено: {icon} {name}!",
        "shop.no_pet": "Нет тени или пользователя",
        "shop.not_enough": "Не хватает {missing} крови 🩸",
        "inv.empty": "📿 Сундук пуст. Загляни в 🕸 Лавку!",
        "notif.pet_sad": "🕸 {name} {reason}",
        "notif.streak_burned": "🕯 Твоя свеча погасла. Зажги новую — напиши что-нибудь в чат!",
        "notif.weekly_prize": "🖤 Ты #{place} в недельном пантеоне шептунов! Дар: 🩸 {prize} капель крови.",
        "common.no_pet": "🜏 У тебя пока нет тени. Нажми /start и пройди обряд!",
        "common.back": "⬅ Назад",
    },
    welcome_text=(
        "🦇 Приветствую, <b>{name}</b>, странник ночи…\n\n"
        "Я — дух-хранитель канала {channel_name}. В этом склебе живут подписчики: "
        "шепотенки, реликвии и немного тёмной магии 🕸\n\n"
        "Что таится внутри:\n"
        "• ⚰ Мерч канала и ночные службы;\n"
        "• 🐈‍⬛ Питомец-тень с гаданиями и ⚔ Ямой;\n"
        "• 💀 Реликвии, ступени, пантеоны недели и 🖼 портрет;\n"
        "• 🌧 Мрачная погода и активность в чате — всё приносит XP и 🩸 капли крови.\n\n"
        "{channel_line}"
        "Нажми «Начать», если осмелишься переступить порог…"
    ),
    main_menu_text=(
        "🏚 <b>Главное меню · {title}</b>\n"
        "\n"
        "👤 {name}, ступень {level} · {bar} {xp}/{need} XP\n"
        "🩸 Капли крови: {coins} · 🕯 Свеча горит: {streak} дн.\n"
        "\n"
        "🗡 Что делать:\n"
        "• 🐈‍⬛ Зайди к кошке-демону — напои её (жажда никуда не делась!)\n"
        "• 🌧 Загляни в /weather — от мрачной погоды Камчатки зависят прогулки:\n"
        "   луна = +находки и 😏 Расположение, ливень/мороз = риск хвори\n"
        "• 🗨 Напиши в чат — засчитывается текст, фото, голос, кружок, стикер\n"
        "• 🖤 Ставь знаки внимания — за них тоже капает XP\n"
        "• 🕸 Копи кровь — лавка (в «🐈‍⬛ Кошка-демон» → «📿 Сундук») и склеп уже ждут\n"
        "• ⚔ Попробуй Яму — еженедельные помрачей теней за дары\n"
    ),
    settings_intro=(
        "🗝 <b>Настройки уведомлений</b>\n"
        "\n"
        "Я шепчу в ЛС только когда это действительно нужно.\n"
        "Здесь можно всё заткнуть — поверни тумблер:\n"
    ),
)

THEMES: dict[str, Theme] = {t.key: t for t in (STANDARD, GOTHIC)}

DEFAULT_THEME_KEY = STANDARD.key


def theme_for_key(key: str | None) -> Theme:
    return THEMES.get((key or "").strip(), STANDARD)


# --- применение темы -------------------------------------------------------

def _map_emoji(text: str, table: dict[str, str]) -> str:
    for src, dst in table.items():
        if src in text:
            text = text.replace(src, dst)
    return text


def gothic(text: str) -> str:
    """Замена стандартных эмодзи на готические (для активной темы)."""
    if current_theme_key() == GOTHIC.key:
        return _map_emoji(text, GOTHIC.emoji_map)
    return text


def tr(key: str, text: str) -> str:
    """Перевод строки на язык активной темы: override по ключу + эмодзи-маппинг."""
    theme = THEMES.get(current_theme_key())
    if theme is None or theme is STANDARD:
        return text
    if key and key in theme.string_overrides:
        return theme.string_overrides[key]
    return _map_emoji(text, theme.emoji_map)


def theme_button_label(cb: str, label: str) -> str:
    theme = THEMES.get(current_theme_key())
    if theme is None or theme is STANDARD:
        return label
    if cb in theme.label_overrides:
        return theme.label_overrides[cb]
    for pattern in theme.label_cb_patterns:
        if fnmatch.fnmatch(cb, pattern):
            return _map_emoji(label, theme.emoji_map)
    # точечные замены по шаблону (например «menu:page:*» — кнопки навигации)
    for pattern, repl in theme.label_overrides.items():
        if "*" in pattern and fnmatch.fnmatch(cb, pattern):
            return repl
    return label


def theme_string_value(key: str, value: str) -> str:
    theme = THEMES.get(current_theme_key())
    if theme is None or theme is STANDARD:
        return value
    return _map_emoji(value, theme.emoji_map)


def welcome_text(user_first_name: str, channel_name: str, channel_line: str) -> str:
    from app.handlers.start import WELCOME_DM  # локальный импорт против цикла
    theme = THEMES.get(current_theme_key())
    template = (theme.welcome_text if theme and theme.welcome_text else WELCOME_DM)
    return template.format(name=user_first_name, channel_name=channel_name,
                           channel_line=channel_line)


def main_menu_renders(**params: Any) -> str | None:
    theme = THEMES.get(current_theme_key())
    if theme is None or theme.main_menu_text is None:
        return None
    try:
        return theme.main_menu_text.format(**params)
    except (KeyError, IndexError):
        return None


def themed_effect(effect):
    """Копия эффекта (Effect из app.utils.fx) с готическими эмодзи и тостом."""
    theme = THEMES.get(current_theme_key())
    if theme is None or theme is STANDARD:
        return effect
    from dataclasses import replace
    return replace(
        effect,
        primary=tuple(_map_emoji(e, theme.emoji_map) for e in effect.primary),
        toast=_map_emoji(effect.toast, theme.emoji_map),
        extra_pool=tuple(_map_emoji(e, theme.emoji_map) for e in effect.extra_pool),
    )


def available_themes() -> list[Theme]:
    return [STANDARD, GOTHIC]
