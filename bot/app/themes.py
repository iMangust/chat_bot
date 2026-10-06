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
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

CURRENT_THEME: contextvars.ContextVar[str] = contextvars.ContextVar(
    "bot_theme", default="standard")

# Кэш «tg_id -> ключ темы» на процесс. Тема меняется редко (только кнопкой
# в настройках), поэтому одно чтение строки users на апдейт — лишняя нагрузка
# на БД; invalidate_theme_cache() вызывается при смене темы и админских
# операциях, сбрасывающих настройки пользователя.
# ВАЖНО: значение None («юзера ещё нет в БД») в кэш НЕ кладётся — иначе
# первый же /start до get_or_create заморозил бы тему как standard навсегда.
# OrderedDict с ручным LRU: обычный dict рос бы без ограничений вместе с
# числом когда-либо виденных пользователей (утечка памяти на аптайме).
_THEME_CACHE: OrderedDict[int, str] = OrderedDict()
_THEME_CACHE_MAX = 50_000


def _theme_cache_put(tg_id: int, theme_key: str) -> None:
    _THEME_CACHE[tg_id] = theme_key
    _THEME_CACHE.move_to_end(tg_id)
    while len(_THEME_CACHE) > _THEME_CACHE_MAX:
        _THEME_CACHE.popitem(last=False)


def _theme_cache_get(tg_id: int) -> str | None:
    key = int(tg_id)
    if key in _THEME_CACHE:
        _THEME_CACHE.move_to_end(key)
        return _THEME_CACHE[key]
    return None

# Последний пользователь, чья тема была установлена middleware'ом.
# Нужен для подстраховки: aiogram 3.x запускает обработчики ошибок
# (ошибка/необработанный callback) в НОВОМ контексте asyncio.Task, где
# contextvar темы НЕ наследуется из задачи апдейта. Без этого фолбэк
# перерисовывал экраны стандартной темой — пользователь видел «тема
# применяется только к настройкам».
_LAST_ACTIVE_TG_ID: int | None = None


def set_theme(name: str | None) -> None:
    CURRENT_THEME.set(THEMES.get(name or "", STANDARD).key)


def remember_theme_owner(tg_id: int | None) -> None:
    """Middleware отмечает, чью тему он только что поставил в контекст."""
    global _LAST_ACTIVE_TG_ID
    _LAST_ACTIVE_TG_ID = int(tg_id) if tg_id is not None else None


def forget_and_remember(tg_id: int, theme_key: str) -> None:
    """Прогрев процессного кэша сразу после смены темы.

    Вызывается из обработчика ``set:theme:*`` ПОСЛЕ коммита в БД:
      1) сбрасывает старое значение кэша для пользователя;
      2) кладёт новый выбор;
      3) запоминает владельца активной темы (для error-handler'ов).

    Это закрывает гонку: aiogram исполняет каждый апдейт в НОВОМ asyncio
    -контексте (contextvar темы там = default standard), и ThemeGuard перед
    хендлером восстанавливает тему только из кэша. Если кэш пуст — guard
    молча оставляет standard, и пользователь видит «тема работает только в
    настройках».
    """
    invalidate_theme_cache(tg_id)
    _theme_cache_put(int(tg_id), theme_key)
    remember_theme_owner(tg_id)


def active_theme_owner() -> int | None:
    return _LAST_ACTIVE_TG_ID


def ensure_theme_for(tg_id: int | None) -> None:
    """Гарантирует корректную тему в текущем контексте для пользователя.

    Если контекст уже показывает ту же тему, что и в кэше — ничего не делаем.
    Иначе (например, мы в новом контексте error-handler'а) переключает
    CURRENT_THEME на сохранённое значение. Вызывается из ThemeErrorMiddleware
    и напрямую из хендлеров, которым важен точный стиль ответа.
    """
    if tg_id is None:
        return
    key = _theme_cache_get(int(tg_id))
    if key is None:
        # В кэша нет — не гадаем: оставляем как есть (для неизвестных юзеров
        # это standard, а свой выбор пользователь всегда в кэше имеет).
        return
    if CURRENT_THEME.get() != key:
        set_theme(key)


def current_theme_key() -> str:
    return CURRENT_THEME.get()


def invalidate_theme_cache(tg_id: int | None = None) -> None:
    if tg_id is None:
        _THEME_CACHE.clear()
    else:
        _THEME_CACHE.pop(int(tg_id), None)


async def load_theme_key(tg_id: int) -> str | None:
    """Ключ темы пользователя из БД (с кэшем на процесс).

    Возвращает ``None``, если юзера в БД ещё нет или тема не выбрана —
    такие значения в кэш НЕ кладутся (см. комментарий у _THEME_CACHE),
    чтобы следующий апдейт прочитал актуальное состояние. Ошибки чтения
    логируются на WARNING: молчаливый откат к standard раньше приводил к
    «тема не применяется нигде, кроме настроек» без единой зацепки в логах.
    """
    tg_id = int(tg_id)
    cached = _theme_cache_get(tg_id)
    if cached is not None:
        return cached
    theme_key: str | None = None
    try:
        from app.db.models import User
        from app.db.session import session_factory

        async with session_factory() as s:
            db_user = await s.get(User, tg_id)
            extra = (db_user.settings_extra or {}) if db_user else {}
            raw = extra.get("theme")
            # JSON-колонка может содержать что угодно (например, после
            # ручной правки) — принимаем только известные ключи тем.
            if isinstance(raw, str) and raw in THEMES:
                theme_key = raw
    except Exception:
        logger.exception("theme: не удалось прочитать тему пользователя %s",
                         tg_id)
        return None
    # ВАЖНО: кэшируем и standard (theme_key=None). Раньше в кэш клались
    # только непустые значения — из-за этого каждый апдейт пользователя без
    # выбранной темы шёл в БД, а любой сбой чтения (например, временная
    # недоступность пула) молча откатывал контекст на standard. Теперь
    # состояние «тема не выбрана» тоже закэшировано; при выборе темы
    # обработчик set:theme:* инвалидирует кэш (invalidate_theme_cache),
    # поэтому устаревание исключено.
    _theme_cache_put(tg_id, theme_key or STANDARD.key)
    return theme_key


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

# Готический шаблон главного меню собирается из канона standard_main_menu_std()
# (см. ниже в модуле). GOTHIC — dataclass frozen, поэтому на время определения
# подставляем заглушку, а сразу после THEMES заменяем её настоящим шаблоном:
# так готика и перекраска старых сообщений (_main_menu_text_from_buttons)
# физически не могут разойтись — источник текста один.
_GOTHIC_MAIN_MENU_PLACEHOLDER = "{title}"  # заглушка на время определения GOTHIC

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
        # «menu:noop» НЕ переопределяем: это невидимая заглушка пагинации,
        # её подпись («🎮 Игра 📖 1/2») служит маркером страницы главного
        # меню при перекраске старых сообщений (_main_menu_text_from_buttons).
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
        "pet.sleeping_deny_wash": "🌑 Во сне не омовляются — пробуди тень, потом соверши обряд.",
        "pet.sleeping_deny_play": "🌑 Спящая тень не играет — разбуди её сначала.",
        "pet.sleeping_deny_toy": "🌑 Игрушки спящим не выдают — сначала пробуди тень.",
        "pet.sleeping_deny_medicine": "🌑 Зелье во сне не дают — разбуди питомца и исцели.",
        "pet.sleeping_deny_walk": "🌑 Тень спит в склебе — в ночной лес она выйдет после пробуждения.",
        "pet.sleeping_deny_duel": "🌑 Спящая тень не сражается — дай ей проснуться.",
        "pet.washed": "🕯 Омовение совершено! 🫧 Гигиена +{hygiene} · 😊 Счастье {happy}",
        "pet.healed": "🧪 Зелье помогло! ❤️ Здоровье +{health}",
        "pet.not_sick": "😈 Питомец здоров, зелье не нужно.",
        # 🤒 Постельный режим больного питомца (механика — _SICK_DENY в
        # app/services/tamagotchi.py); ключи должны существовать и в i18n.
        "pet.sick_deny": "🌑 Тень больна — нужен покой в склепе! Сначала зелье "
                         "(🧪 «Лечить») и здоровый сон.",
        "pet.sick_deny_game": "🌑 Больная тень не играет — игры подождут до исцеления. "
                              "Дай ей зелье (🧪) и позволь выспаться.",
        "pet.sick_deny_train": "🌑 Больной тени тренировки противопоказаны — сначала "
                               "исцели её (🧪), потом продолжим.",
        "pet.sick_deny_walk": "🌑 В ночной лес больной не ходят. Исцели тень (🧪) "
                              "— и вместе погуляете.",
        "pet.sick_deny_duel": "🌑 Больная тень на арену не выходит — дай ей "
                              "выздороветь (🧪 + сон).",
        "pet.walk_started": "🌲 Тень ушла в ночной лес на {hours} ч. Вернётся в {time} — с новостями!",
        "pet.walk_already": "🌲 Тень уже бродит по лесу… Вернётся в {time} (через {minutes}).",
        "pet.not_walking": "🏚 Она и не гуляет — дома, в склебе.",
        "pet.walk_returned_early": "🌲 Ты позвал тень домой! Бродила {hours} ч → {reward} (уже учтено на карточке).",
        "pet.walk_back_line": "🌲 Сейчас в лесу… Вернётся в {time} (через {minutes}).",
        "pet.walk_deny_sleep": "🌲 {name} бродит — спать на кладбище нельзя! Дождись возвращения.",
        "pet.walk_deny_wash": "🕯 {name} бродит — омовение на кладбище негде совершить! Дождись возвращения.",
        "pet.walk_deny_train": "⚔ {name} бродит — тренироваться на кладбище негде! Дождись возвращения.",
        "pet.walk_deny_medicine": "🧪 {name} бродит — зелья на прогулке не дают! Дождись возвращения.",
        "pet.walk_deny_feed": "🍷 {name} бродит — покормить её дома некому! Дождись возвращения.",
        "pet.walk_deny_heal": "🧪 {name} бродит — лечение совершается дома, дождись возвращения.",
        "pet.walk_deny_game": "🎾 {name} бродит — игры ждут в склебе! Дождись возвращения.",
        "pet.walk_deny_toy": "🧸 {name} бродит — игрушку оставь до её возвращения.",
        "pet.walk_deny_duel": "🏟 {name} бродит — на поединок её не вытащить! Дождись возвращения.",
        "pet.walk_deny_walk": "🌲 {name} уже бродит — пусть дойдёт до склеба!",
        "pet.walk_deny_play": "🎾 {name} бродит — игры ждут в склебе! Дождись возвращения.",
        "pet.walk_deny_item": "🎒 {name} бродит — вещи выдаём дома, дождись возвращения.",
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
        "pet.revive_done": "🕯 {name} восстала из пепла! Следующий обряд будет дороже.",
        "pet.revive_free": "🎁 Первый обряд — даром! Береги тень 💜",
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
    main_menu_text=_GOTHIC_MAIN_MENU_PLACEHOLDER,
    settings_intro=(
        "🗝 <b>Настройки</b>\n"
        "\n"
        "Я шепчу в ЛС только когда это действительно нужно.\n"
        "Здесь можно всё заткнуть — поверни тумблер:\n"
    ),
)


DEFAULT_THEME_KEY = STANDARD.key


def theme_for_key(key: str | None) -> Theme:
    return THEMES.get((key or "").strip(), STANDARD)


# --- применение темы -------------------------------------------------------

def _map_emoji(text: str, table: dict[str, str]) -> str:
    # Сортировка по убыванию длины ключа обязательна: иначе короткий ключ
    # съедает длинный раньше времени. Например «🏋» подменялся внутри
    # «🏋️ Тренировки», и оставшийся вариант-селектор превращал подпись в
    # битый эмодзи «⚔️‍».
    for src in sorted(table, key=len, reverse=True):
        dst = table[src]
        if src in text:
            text = text.replace(src, dst)
    return text


def standard_main_menu_std() -> str:
    """Канонический СТАНДАРТНЫЙ текст главного меню с маркерами {title}/{stats}.

    Единственный источник правды для готического шаблона и для перекраски
    старых сообщений — раньше они были скопированы друг из друга в двух
    местах и устаревали при изменении меню (пользователь видел «тема не
    применяется»: подсказки в готике расходились со стандартом).

    Строка «🐾 Питомец: …» подставляется на лету (_main_menu_pet_line в
    start.py): канон содержит только маркер {pet}, а его готическая копия —
    строку-заглушку, чтобы перекраска старых сообщений не показывала пустую
    строку (питомцев там переставляет уже живой рендер).
    Мерч/ивенты из ликбеза убраны: главное меню — про тамагочи и XP,
    а витрина магазина не относится к уходу за питомцем.
    Строки показателей — маркеры с ПОЛНЫМИ именами плейсхолдеров
    («👤 {name}, уровень {level} · {bar} {xp}/{need} XP», «🪙 Монеты:
    {coins} · 🔥 Серия: {streak} дн.»), а не один {stats}: раньше live-рендер
    format'ил канон с готовой строкой stats целиком, и готический шаблон
    терял замены темы («ступень/Капли крови»), потому что _gothic_stats
    работал построчно и не мог подменить уже отформатированный блок
    (регрессия 2026-10, тест test_set_theme_gothic_persists_and_recolors_menu).
    Теперь порядок одинаковый для обеих тем: сначала подмена строк шаблона
    (_gothic_stats/_gothic_phrases при сборке готики), затем format() живыми
    значениями. Перекраска старых сообщений
    (settings._main_menu_text_from_buttons) передаёт сохранённые показатели
    теми же параметрами.
    """
    from app.keyboards.inline import MENU_PAGES  # noqa: F401 (синхронность с меню)
    lines = [
        "👋 Привет, {name}! Твоя крепость ждёт. 🏰",
        "",
        "👤 {name}, уровень {level} · {bar} {xp}/{need} XP",
        "🪙 Монеты: {coins} · 🔥 Серия: {streak} дн.",
        "",
        "🐾 Твой питомец:",
        "{pet}",
        "",
        "📌 Краткий ликбез:",
        "• 🐾 Зайди в «Питомец» → покорми, поиграй, погуляй — статы падают",
        "   даже без тебя; полный гид ухода живёт во вкладке «🧴 Уход»",
        "• 🌦️ Погода влияет на прогулки: солнце = +находки и 😊 Счастье,",
        "   дождь/мороз = риск простуды (кнопка «Погода» или /weather)",
        "• 💬 Напиши в чат — засчитывается текст, фото, голос, кружок, стикер",
        "• ❤️ Ставь реакции — за них тоже капает XP",
        "• ⚔️ Попробуй Арену — еженедельные дуэли питомцев за призы",
    ]
    body = "\n".join(lines)
    ch_line = ""
    try:
        from app.middlewares.gate import channel_link
        ch, visual = channel_link()
        if ch:
            ch_line = f"\n\n📢 Новости канала: {visual} (t.me/{ch})"
    except Exception as exc:
        logger.debug("Главное меню: не удалось подставить строку канала: %s", exc)
    # Маркеры показателей («👤 {name}, уровень …», «🪙 Монеты: …») остаются
    # в шаблоне как есть: живые значения подставляет format() (см.
    # main_menu_renders и start._main_menu_text), а готические фразы —
    # _gothic_stats на этапе сборки шаблона. Перекраска старых сообщений
    # (settings._main_menu_text_from_buttons) передаёт сохранённые показатели
    # теми же параметрами.
    return "🏠 <b>Главное меню · {title}</b>\n\n" + body + ch_line


# Готические фразы строк показателей главного меню (ключ = строка канона ДО
# двоеточия, значение = готическая копия с ТЕМИ ЖЕ маркерами). Раньше эти
# фразы были вшиты в literal-шаблон GOTHIC.main_menu_text; после перевода
# шаблонов на канон standard_main_menu_std() их нужно восстанавливать здесь,
# иначе из готики исчезают «ступень/Капли крови» (тест
# test_set_theme_gothic_persists_and_recolors_menu). Подмена выполняется по
# СТРОКАМ ШАБЛОНА (до format() и до эмодзи-маппинга), поэтому маркеры
# сохраняются и живые значения подставляются уже готической строкой.
_GOTHIC_STATS_LINES: dict[str, str] = {
    "👤": "👤 {name}, ступень {level} · {bar} {xp}/{need} XP",
    "🪙 Монеты": "🩸 Капли крови: {coins} · 🕯 Свеча горит: {streak} дн.",
}


def _gothic_stats(text: str) -> str:
    """Заменяет стандартные строки показателей на готические фразами темы.

    Сравнение по префиксу строки («🪙 Монеты»): при изменении вёрстки канона
    замена молча не сработает, но её контролирует тест gothic-меню —
    регрессия не пройдёт CI.
    """
    out = []
    for ln in text.splitlines():
        repl = next((v for k, v in _GOTHIC_STATS_LINES.items()
                     if ln.startswith(k)), None)
        out.append(repl if repl is not None else ln)
    return "\n".join(out)


# Готическое приветствие главного меню: канон standard_main_menu_std()
# содержит нейтральное «Твоя крепость ждёт 🏰», в готике — склеп. Раньше
# эта фраза была вшита в literal-шаблон GOTHIC.main_menu_text; после
# перевода шаблонов на канон её нужно восстанавливать здесь (иначе живое
# меню в готике говорит «крепость» вместо «склепа»). Ключ — фрагмент
# канона ДО подстановки имени, поэтому сверка устойчива к вёрстке строки.
_GOTHIC_PHRASES: dict[str, str] = {
    "Твоя крепость ждёт. 🏰": "Твой склеп ждёт. 🕯",
    # Названия разделов в ликбезе: emoji_map перекрашивает только эмодзи
    # («🐾»→«🕸»), слова остаются стандартными и противоречат реальным
    # готическим подписям кнопок (inline.py: «🐈‍⬛ Кошка-демон»,
    # «🕯 Уход»). Регрессия на жалобу «тема не применяется»: подсказки
    # должны вести именно туда, как кнопки названы в теме.
    "Зайди в «Питомец» →": "Зайди в «🐈‍⬛ Кошку-демона» →",
    # Заглушка «питомца пока нет» упоминает раздел «🐾 Питомец» — в готике
    # кнопки переименованы («🐈‍⬛ Кошка-демон»), и тест требует отсутствия
    # стандартного названия в тексте. Подставляем готическое имя раздела.
    "заведи его в «🐾 Питомец»": "заведи её в «🐈‍⬛ Кошку-демона»",
}


def _theme_phrases(text: str, theme: Theme) -> str:
    """Подменяет нейтральные фразы канона на аналоги темы.

    Для готики таблица = _GOTHIC_PHRASES; у других тем пока нет фраз-замен.
    """
    table = _GOTHIC_PHRASES if theme is GOTHIC else {}
    for src, dst in table.items():
        if src in text:
            text = text.replace(src, dst)
    return text


def _gothic_phrases(text: str) -> str:
    """Подменяет нейтральные фразы канона на готические аналоги."""
    return _theme_phrases(text, GOTHIC)


def _build_main_menu_template() -> str:
    """Готический шаблон текста главного меню — из канонического стандартного.

    Порядок важен: сначала подмена СТРОК ПОКАЗАТЕЛЕЙ на готические фразы
    («ступень/Капли крови») по чистым строкам канона, затем маппинг эмодзи
    (🪙→🩸 и т.п.) и нейтральных фраз («крепость»→«склеп»). Если менять
    порядок наоборот, _gothic_stats не найдёт «🪙 Монеты: …» (эмодзи уже
    заменён) и в готике останутся стандартные слова при готических
    эмодзи-маркерах (регрессия 2026-10).
    """
    return _gothic_phrases(
        _map_emoji(_gothic_stats(standard_main_menu_std()), GOTHIC.emoji_map))


# Замыкаем схему «один источник правды»: готический шаблон главного меню
# собирается из канона standard_main_menu_std() сразу, как только он и
# _map_emoji определены ниже в модуле. GOTHIC — frozen dataclass, поэтому
# замена через dataclasses.replace; THEMES строится уже из «доведённой»
# темы, чтобы нигде не осталось подстановочной заглушки "{title}".
from dataclasses import replace as _dc_replace

GOTHIC = _dc_replace(GOTHIC, main_menu_text=_build_main_menu_template())

THEMES: dict[str, Theme] = {t.key: t for t in (STANDARD, GOTHIC)}


def _active_theme() -> Theme | None:
    """Тема текущего контекста с подстраховкой против «потерянного» contextvar.

    aiogram 3.x может вызвать рендер вне задачи апдейта (error-handler'ы,
    фолбэки, таски планировщика) — там CURRENT_THEME == default(standard),
    хотя пользователь выбрал другую тему. Если контекст пустой, а у
    последнего активного пользователя в кэше лежит выбор — восстанавливаем его.
    """
    if (current_theme_key() == STANDARD.key
            and _LAST_ACTIVE_TG_ID is not None):
        ensure_theme_for(_LAST_ACTIVE_TG_ID)
    return THEMES.get(current_theme_key())


def gothic(text: str) -> str:
    """Замена стандартных эмодзи на готические (для активной темы)."""
    if current_theme_key() == GOTHIC.key:
        return _map_emoji(text, GOTHIC.emoji_map)
    return text


def tr(key: str, text: str) -> str:
    """Перевод строки на язык активной темы: override по ключу + эмодзи-маппинг."""
    theme = _active_theme()
    if theme is None or theme is STANDARD:
        return text
    if key and key in theme.string_overrides:
        return theme.string_overrides[key]
    return _map_emoji(text, theme.emoji_map)


def theme_button_label(cb: str, label: str) -> str:
    theme = _active_theme()
    if theme is None or theme is STANDARD:
        return label
    # 1) Полное совпадение callback_data — самый сильный приоритет.
    #    Так работают точечные переименования («🐾 Питомец» → «🐈‍⬛ Кошка-демон»).
    if cb in theme.label_overrides:
        return theme.label_overrides[cb]
    # 2) Точечная замена по шаблону («menu:page:*» и т.п.) — раньше проверялась
    #    ПОСЛЕ глобального эмодзи-маппинга и до неё никогда не доходило:
    #    «menu:page:0» подходил под паттерн «menu:*» и возвращался уже
    #    перемешенный текст вместо задуманной готической подписи.
    for pattern, repl in theme.label_overrides.items():
        if "*" in pattern and fnmatch.fnmatch(cb, pattern):
            return repl
    # 3) Глобальная замена эмодзи для кнопок из тематизируемых разделов.
    for pattern in theme.label_cb_patterns:
        if fnmatch.fnmatch(cb, pattern):
            return _map_emoji(label, theme.emoji_map)
    return label


def theme_string_value(key: str, value: str) -> str:
    theme = _active_theme()
    if theme is None or theme is STANDARD:
        return value
    return _map_emoji(value, theme.emoji_map)


def welcome_text(user_first_name: str, channel_name: str, channel_line: str) -> str:
    from app.handlers.start import WELCOME_DM  # локальный импорт против цикла
    theme = _active_theme()
    template = (theme.welcome_text if theme and theme.welcome_text else WELCOME_DM)
    return template.format(name=user_first_name, channel_name=channel_name,
                           channel_line=channel_line)


def main_menu_renders(pet_line: str = "", **params: Any) -> str | None:
    theme = _active_theme()
    if theme is None or theme.main_menu_text is None:
        return None
    # Маркер строки питомца подставляем ДО format(): он часть шаблона, и
    # format() падал бы с KeyError 'pet' (тема молча «не применялась»).
    # Значение может содержать фигурные скобки (emoji-варианты, имена с «{}»)
    # — экранируем их, чтобы format() пропустил текст как литерал.
    # Пустая строка (питомца нет / карточка недоступна) не должна оставлять
    # висящий маркер "{pet}" — иначе пользователь видит сырой шаблон.
    # Заглушка «питомца пока нет» приходит из start.py стандартной («🐾 Питомец»),
    # а в готике кнопки переименованы — тест требует отсутствия стандартного
    # названия. Применяем фразы темы и к строке питомца: она подставляется уже
    # собранного шаблона, поэтому _gothic_phrases (работает при сборке) до неё
    # не доходит.
    pet_text = _theme_phrases(pet_line, theme)
    params["pet"] = (pet_text.replace("{", "{{").replace("}", "}}")
                     if pet_text else "")
    try:
        out = theme.main_menu_text.format(**params)
    except (KeyError, IndexError):
        return None
    # Пустая строка питомца (готика без карточки) оставляет «заголовок +
    # пустой {pet}» — схлопываем двойные пустые строки в одну.
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    return out


def themed_effect(effect):
    """Копия эффекта (Effect из app.utils.fx) с готическими эмодзи и тостом."""
    theme = _active_theme()
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
