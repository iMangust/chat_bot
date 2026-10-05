from __future__ import annotations

from aiogram.types import InlineKeyboardButton as _IKB
from aiogram.types import InlineKeyboardMarkup as _IKM
from aiogram.utils.keyboard import InlineKeyboardBuilder as _RawIKB
from loguru import logger


def _themed(values: dict) -> dict:
    """Подменяет text кнопки под активную тему оформления (см. app/themes.py)."""
    cb = values.get("callback_data")
    text = values.get("text")
    if isinstance(cb, str) and isinstance(text, str) and cb:
        from app import themes
        values["text"] = themes.theme_button_label(cb, text)
    return values


class _ThemedInlineKeyboardButton(_IKB):
    """InlineKeyboardButton, подменяющий текст под активную тему оформления.

    aiogram 3.x создаёт модели через ``TypeBase.model_validate`` /
    ``model_construct`` (прямой ``__init__`` у pydantic-моделей не вызывается),
    поэтому перехватываем ВСЕ точки входа. Так тематизируются кнопки ВЕЗДЕ
    (прямые конструкторы и ``InlineKeyboardBuilder.button(...)``) без правки
    сотен мест — достаточно использовать этот класс в модулях клавиатур.
    """

    @classmethod
    def model_validate(cls, obj, *args, **kwargs):
        if isinstance(obj, dict):
            obj = _themed(dict(obj))
        return super().model_validate(obj, *args, **kwargs)

    @classmethod
    def model_construct(cls, _fields_set=None, **values):
        return super().model_construct(_fields_set=_fields_set,
                                       **_themed(values))

    # Pydantic v2 при обычном вызове конструктора (Button(text=..., ...))
    # обходит класс-методы и идёт сразу в __init__ — перехватываем и его.
    def __init__(self, **data):
        super().__init__(**_themed(data))

    # Но есть ИСКЛЮЧЕНИЕ: когда кнопка вложена в другую pydantic-модель
    # (InlineKeyboardMarkup.inline_keyboard), pydantic v2 валидирует её
    # напрямую через core-schema класса — model_validate/__init__ НЕ
    # вызываются. Именно поэтому раньше тема «применялась только к
    # настройкам»: клавиатуры, собранные вручную (main_menu, pet_hub и др.
    # через InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(...)]])
    # или переданные dict'ами), проходили мимо всех перехватчиков.
    # Перехватываем и эту точку входа: after-validator получает на вход
    # УЖЕ валидированный экземпляр кнопки (или dict из model_construct-пути)
    # — достаточно переписать ему text под активную тему.
    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        schema = super().__get_pydantic_core_schema__(source_type, handler)
        from pydantic_core import core_schema as cs

        def _retheme(v, _info):
            cb = getattr(v, "callback_data", None)
            text = getattr(v, "text", None)
            if isinstance(cb, str) and isinstance(text, str) and cb:
                from app import themes
                new_text = themes.theme_button_label(cb, text)
                if new_text != text:
                    try:
                        object.__setattr__(v, "text", new_text)
                        fields = getattr(v, "__pydantic_fields_set__", None)
                        if fields is not None:
                            fields.add("text")
                    except Exception as exc:
                        logger.debug("theme button relabel skipped for {!r}: {!r}", cb, exc)
            return v

        return cs.with_info_after_validator_function(_retheme, schema)


class InlineKeyboardBuilder(_RawIKB):
    """Билдер, собирающий тематизированные кнопки (см. app/themes.py)."""

    BUTTON_TYPE = _ThemedInlineKeyboardButton


# Тематизированный Markup: ловит кнопки даже когда их передают списком
# dict'ов («[[{"text": ..., "callback_data": ...}]]») — в этом случае
# pydantic вообще не видит наш класс кнопок, и без этого перехвата подписи
# остались бы стандартными.
class _ThemedInlineKeyboardMarkup(_IKM):
    @classmethod
    def model_validate(cls, obj, *args, **kwargs):
        if isinstance(obj, dict) and isinstance(obj.get("inline_keyboard"), list):
            obj = dict(obj)
            obj["inline_keyboard"] = [
                [b.model_dump(exclude_none=True) if isinstance(b, _IKB) else b
                 for b in row] if isinstance(row, list) else row
                for row in obj["inline_keyboard"]]
        return super().model_validate(obj, *args, **kwargs)


# Дальнейший код модуля (и все импортирующие его модули) используют
# тематизированные кнопку и markup вместо оригиналов.
InlineKeyboardButton = _ThemedInlineKeyboardButton

InlineKeyboardMarkup = _ThemedInlineKeyboardMarkup
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
        # «menu:noop» — заглушка без действия; её подпись в теме НЕ должна
        # переопределяться (см. themes.theme_button_label: точное совпадение
        # с ключом вида «X:noop» пропускается), чтобы по ней можно было
        # опознать страницу главного меню при перекраске старых сообщений.
        InlineKeyboardButton(text=label, callback_data=f"{prefix}:noop"),
        InlineKeyboardButton(text="▶️", callback_data=next_cb),
    ]

MENU_PAGES: list[tuple[str, list[tuple[str, str]]]] = [
    ("🎮 Игра", [
        ("🐾 Питомец", "menu:pet"),
        ("🧢 Наш мерч", "menu:merch"),
        ("📅 Мероприятия", "menu:events"),
        # Кнопка «Погода»: подпись динамически перекрашивается в актуальную
        # погоду («🌦 +3° Дождь») — см. update_weather_button / main_menu.
        ("☀️ Погода", "menu:weather"),
        # 📖 «Гид по уходу» убран из главного меню: это справочный раздел
        # именно про питомца, поэтому его кнопка живёт в хабе питомца
        # (PET_PAGES → вкладка «🧴 Уход»). Главное меню остаётся чистым.
    ]),
    ("👤 Профиль", [
        ("📊 Статистика", "menu:stats"),
        ("🏆 Награды", "menu:ach"),
        ("🏅 Топы", "menu:top"),
        ("🖼 Карточка", "menu:card"),
        # Экран «Настройки» теперь объединяет уведомления и выбор темы
        # оформления — кнопка переименована, чтобы не вводить в заблуждение.
        ("⚙️ Настройки", "menu:settings"),
    ]),
]

def menu_page_count() -> int:
    return len(MENU_PAGES)

ADMIN_TOOLS_PAGE = ("🛠 Инструменты админа", [
    ("🧢 Управление мерчем", "madmin:home"),
    ("📅 Управление мероприятиями", "evadmin:home"),
    ("⚙️ Мои настройки", "menu:settings"),
])

def main_menu(link: str | None = None, reward: int = 0,
              page: int = 0, is_admin: bool = False,
              weather_label: str | None = None) -> InlineKeyboardMarkup:
    settings = get_settings()
    pages = list(MENU_PAGES)
    if is_admin:
        pages.append(ADMIN_TOOLS_PAGE)
    page %= len(pages)
    title, actions = pages[page]
    buttons = [InlineKeyboardButton(text=t, callback_data=cb)
               for t, cb in actions
               if not (cb == "menu:merch" and not settings.merch_enabled)]
    # Кнопка «☀️ Погода» показывает актуальную погоду прямо в подписи
    # («🌦 +3° Дождь»). Метка приходит извне (update_weather_button /
    # кэш сервиса), чтобы сборка меню никогда не делала сетевой запрос.
    if weather_label:
        for btn in buttons:
            if btn.callback_data == "menu:weather":
                btn.text = weather_label[:64]
    kb_rows: list[list[InlineKeyboardButton]] = _two_per_row(buttons)
    # Постраничная навигация меню (◀️ 📖 ▶️). Отдельного CTA «🥚 Усыновить
    # питомца» здесь нет: усыновление живёт в разделе питомца — кнопка
    # «🐾 Питомец» ведёт в хаб, где без питомца показывается экран с
    # предложением усыновить (adopt_cta_kb). Главное меню остаётся чистым.
    kb_rows.append(_page_nav("menu", page, len(pages), title))
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
        # 📖 Гид по уходу (manual:home) — справочный раздел про питомца:
        # виды, что любят/не любят, как качать статы, игры и экипировка.
        # Живёт здесь (а не в главном меню): кнопка ведёт из хаба питомца.
        ("📖 Гид по уходу", "manual:home"),
        # 📜 История питомцев — ОДНА кнопка, только на вкладке «Уход»
        # (первая страница хаба). На других вкладках и подразделах она
        # не дублируется: туда ведёт «⬅️ Назад» → эта же вкладка.
        ("📜 История питомцев", "pet:history"),
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

# Кнопки-состояния НЕ прячутся и не переключаются: все действия видны
# всегда, а запрет работает только в момент тапа — единый всплывающий
# отказ (alert) от стража state_deny в handlers. Это гарантирует, что
# «🚶 Прогулка» → «🏠 Вернуть с прогулки» доступны одновременно на всех
# вкладках (раньше кнопка возврата терялась при перерисовке экрана).
STATE_EXTRA_BUTTONS: list[tuple[str, str]] = [
    ("⏰ Разбудить", "pet:wake"),
    ("🏠 Вернуть с прогулки", "pet:end_walk"),
]

def _state_extra_row(sleeping: bool, walking: bool):
    """Дополнительная строка кнопок выхода из состояний (если активны)."""
    row = [InlineKeyboardButton(text=txt, callback_data=cb)
           for (txt, cb), active in zip(STATE_EXTRA_BUTTONS, (sleeping, walking))
           if active]
    return row or None

def pet_hub(page: int = 0, critical: bool = False,
            sleeping: bool = False, walking: bool = False) -> InlineKeyboardMarkup:
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
    # «📜 История питомцев» НЕ добавляется сюда ко всем вкладкам: кнопка
    # живёт в одном месте — на вкладке «Уход» (PET_PAGES, страница 0).
    kb_rows = _two_per_row(buttons)
    # Кнопки выхода из состояний — отдельной строкой, на ЛЮБОЙ вкладке,
    # когда состояние активно. Ничего не прячем: остальные действия
    # остаются видимыми, а отказ при тапе даёт единый всплывающий alert.
    extra = _state_extra_row(sleeping, walking)
    if extra:
        kb_rows.append(extra)
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
_PET_SUB_SECTIONS = frozenset({"games", "arena", "friends", "shop",
                               "inv", "style", "pet"})

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
    # 'menu:<раздел>' — вход в НЕКОТОРЫЙ раздел главного меню. Для
    # подразделов хаба питомца повторный 'menu:pet' — это тот же хаб
    # (листание/повторный вход), а не выход наружу. Для прочих секций
    # 'menu:...' — это обычные точки возврата из истории (их фильтрует
    # поиск по индексу точки входа, а не этот предикат).
    return bool(head == "menu" and cb == "menu:pet" and sec in _PET_SUB_SECTIONS)


def _nav_back_cb(section: str | None, chat_id: int | None,
                 current_cb: str | None = None) -> str | None:
    """Callback для кнопки «⬅️ Назад»: ближайшая подходящая запись стека
    навигации (экран, откуда пришли на этот), а если стек пуст или вся
    история — «свой» раздел — безопасный корень раздела. Синхронная версия
    читает локальное зеркало стека; актуальность обеспечивает
    NavStackMiddleware, который после каждого коллбэка перечитывает
    Redis-стек в зеркало (см. middlewares/nav_stack.py).

    КРИТИЧНО: результат никогда не должен совпадать с текущим экраном —
    иначе нажатие «Назад» шлёт тот же callback и Telegram показывает
    «кнопка неактивна / ничего не происходит». Поэтому корень раздела
    исключается из кандидатов ещё ДО поиска по истории: он — пропускной
    пункт (в него мы зашли), а не пункт назначения.

    ДВА РЕЖИМА ИСТОЧНИКА СТЕКА (важно для тестов и прода):
      • В проде middleware кладёт НАЖАТУЮ кнопку (источник перехода) ещё
        ДО отрисовки экрана — вершина стека = экран, ОТКУДА пришли, а сам
        текущий экран в стеке отсутствует.
      • Если обработчик перерисовывает экран ПОСЛЕ своей кнопки (или тест
        пишет в стек сам), вершиной оказывается текущий экран — такую
        запись надо скипнуть, иначе «Назад» ведёт в себя («ничего не
        происходит»). Реализовано ниже через `top_is_current`."""
    from app.utils import nav as _nav
    stack = _nav.mem_stack(chat_id)

    # ── Режим 0: «источник перехода» передан явно (current_cb НЕ равен
    # нажатой кнопке, а обработчик знает, откуда открылся экран). Не
    # используется здесь; оставлено для ясности. ──
    #
    # ── Режим prod-модели (основной): NavStackMiddleware кладёт НАЖАТУЮ
    # кнопку в стек ДО отрисовки экрана. Если обработчик вызвал
    # build_screen(current_cb=X), значит пользователь нажал X и теперь
    # смотрит на экран X → вершина стека == X == текущий экран. Тогда
    # «экран, ОТКУДА пришли» — это stack[-2] (предпоследняя запись), а не
    # stack[-1]. Этот случай обрабатывается ниже через `top_is_current`.
    #
    # ── Режим self-model (перерисовка без нового перехода): обработчик
    # обновляет уже открытый экран (например, выбран другой размер того
    # же товара) и вызывает build_screen(current_cb=Y), где Y — ЭКРАН, а
    # не нажатая кнопка; в стеке поверх лежит нажатая 'merch:size:M'.
    # Тогда вершина stack[-1] ≠ current и является источником перехода. ──
    root = SECTION_ROOTS.get(section or "", "menu:main")

    # ── Режим current_source (проверенный паттерн хаба питомца) ──
    # Если обработчик явно передал current_cb == нажатая кнопка (pet:style,
    # pet:shop, pet:inv, game:rps…), а middleware уже положил её в стек ДО
    # отрисовки экрана — источник перехода лежит ГЛУБЖЕ точки входа. Ищем
    # последнюю запись стека, НЕ относящуюся к текущему разделу, и ведём
    # «Назад» туда. Это гарантирует возврат на вкладку хаба («🎒 Вещи»,
    # «🎮 Досуг»), а не в главное меню.
    def _pet_sub_tab_back() -> str | None:
        """Точка возврата «на уровень выше» для подраздела хаба питомца.

        Подразделы («Стиль», «Магазин», «Инвентарь», «Игры») живут
        ОТДЕЛЬНЫМИ сообщениями поверх вкладки хаба, поэтому возврат на
        вкладку ('pet:page:N') — реальный переход вверх, а не самопетля.
        Берём вкладку из истории; если её нет — дефолт по корню секции.
        Раньше эти записи считались «своими» (_SECTION_OWN_PREFIXES
        включает 'pet'), «Назад» исчезал совсем или улетал в главное меню.
        """
        if section not in _PET_SUB_SECTIONS or section == "pet":
            return None
        for j in range(len(stack) - 1, -1, -1):
            e = stack[j]
            if e and e.startswith("pet:page:"):
                return e
        # Истории нет (deep-link / рестарт без Redis) — дефолтная вкладка
        # раздела: магазин/инвентарь/стиль ← «🎒 Вещи», игры/друзья/арена ←
        # «🎮 Досуг». Иначе «Назад» не строился вовсе и пользователь мог
        # уйти только «🏠 Домой» (главное меню), минуя хаб.
        default_tab = SECTION_ROOTS.get(section or "")
        if default_tab and default_tab.startswith("pet:page:"):
            return default_tab
        return root if root.startswith("pet:page:") else None

    if current_cb and stack and stack[-1] == current_cb:
        # Исключение: 'menu:page:N' (листание страниц главного меню).
        # Если пользователь перелистнул меню, а потом открыл подраздел
        # ('pet:shop'), то 'menu:page:0' ≠ 'menu:main' по тексту, но это
        # всё ещё то же меню. Возврат на него = перерисовка текущего
        # экрана → самопетля. Подавляем «Назад», оставляем только «Меню».
        if current_cb.startswith("menu:page:"):
            return None
        # Подраздел хаба питомца: источник перехода — вкладка хаба
        # ('pet:page:N'), даже если формально она «своя» по префиксам.
        tab = _pet_sub_tab_back()
        if tab and tab != current_cb:
            return tab
        for j in range(len(stack) - 2, -1, -1):
            e = stack[j]
            if not e or e.endswith(":noop") or e == "noop":
                continue
            if _is_own_section_entry(e, section):
                continue
            if e in ("menu:main", "menu:home"):
                break
            return e
        # Стек пуст или вся история — «свой» раздел. Для верхнеуровневых
        # экранов («Статистика», настройки…) возврат в корень раздела =
        # перерисовка того же экрана («Назад ничего не делает») → подавляем.
        # Но для ПОДРАЗДЕЛОВ хаба питомца («Стиль», «Магазин», «Игры»)
        # осмысленный выход на уровень выше существует — вкладка хаба,
        # которая и является корнем секции ('pet:page:1' / 'pet:page:2'):
        # показываем «Назад» туда вместо безликого «🏠 Меню».
        if root != "menu:main" and root.startswith("pet:page:"):
            return root
        return None
    # Плоский раздел (топы, достижения): многоуровней нет — «Назад» не
    # показываем вообще, даже если пользователь зашёл из другого раздела
    # (Мерч → Топы). Единственный выход — «🏠 Меню»: так экраны остаются
    # предсказуемыми и без кнопок-петель.
    if _back_suppressed(section):
        return None
    # Внутренняя история раздела (вкладки/подразделы одного экрана) — не
    # источник «Назад наружу»: у таких секций корень лежит ВНУТРИ того же
    # сообщения (хаб питомца: 'pet:page:*', 'pet:games'), возврат туда =
    # перерисовка текущего экрана («Назад ничего не делает»). Считаем
    # историю «своей», если в ней нет ни одной записи вне раздела.
    def _is_internal_entry(e: str) -> bool:
        # 'pet:page:N' — вкладка хаба, а НЕ экран подраздела: подраздел
        # («Магазин», «Игры», «Стиль») открывается ОТДЕЛЬНЫМ сообщением
        # поверх вкладки, поэтому вкладка — реальный источник перехода и
        # валидная точка возврата («Назад» на неё возвращает к вкладке).
        # Раньше она считалась «своей» историей, весь стек признавался
        # внутренним, «Назад» подавлялся — и из подразделов оставался
        # только безликий «🏠 Домой».
        if e.startswith("pet:page:") and section in _PET_SUB_SECTIONS \
                and section != "pet":
            return False
        return (_is_own_section_entry(e, section)
                or (section in _PET_SUB_SECTIONS and e.startswith("pet:"))
                or (not section and e.startswith("menu:page:")))
    own_history = bool(stack) and all(_is_internal_entry(e) for e in stack)
    if not stack or own_history:
        # Нет истории: возврат в корень раздела уместен только там, где
        # сам экран НЕ является этим корнем (корневой список мерча/событий —
        # «Назад» ведёт в корень раздела, а не в главное меню). Для
        # подстраниц (категория/товар) и верхнеуровневых экранов
        # («Статистика», карточка, настройки) корень = текущий экран либо
        # возврат «в себя» бессмыслен → только «🏠 Меню».
        is_self_root = (root == f"menu:{section}"
                        or (section == "pet" and root.startswith("pet:")))
        if is_self_root:
            return None
        if current_cb:
            # Подстраница своего раздела (merch:cat:* при section='merch'):
            # «Назад» в корень = самопетля на уже открытом экране.
            head = str(current_cb).split(":")[0]
            if section and head == section:
                return None
        return root

    def _find_last(items: list[str], value: str) -> int | None:
        for i in range(len(items) - 1, -1, -1):
            if items[i] == value:
                return i
        return None

    def _find_last_prefix(items: list[str], prefixes: tuple[str, ...]) -> int | None:
        for i in range(len(items) - 1, -1, -1):
            if items[i] and any(items[i].startswith(p) for p in prefixes):
                return i
        return None

    def _effective_root() -> str:
        """Корень раздела с учётом вкладки хаба питомца, по которой пришли.

        Подразделы хаба («Магазин», «Инвентарь», «Стиль», «Игры»…) живут
        ОТДЕЛЬНЫМИ сообщениями поверх вкладки, поэтому реальный «Назад на
        уровень выше» — вкладка хаба ('pet:page:N'), по которой пришли.
        Раньше здесь возвращалась КНОПКА входа ('pet:shop'), которая равна
        текущему экрану: проверка самопетли ниже гасила её в menu:main —
        и вместо возврата к вкладке пользователь получал только «🏠 Домой».
        Для секций вне хаба — просто корень раздела."""
        if section in _PET_SUB_SECTIONS and section != "pet":
            tab = _pet_sub_tab_back()
            if tab:
                return tab
        return root

    root = _effective_root()

    # Самопетля недопустима: если «корень на уровень выше» совпадает с
    # текущим экраном (open-root-модель обработчиков: merch:cat открыт
    # кнопкой 'merch:cat' и корнем для него служит сам 'merch:cat'; либо
    # merch:prod, для которого 'merch:cat:N' — вершина стека), возврат в
    # такой корень перерисовал бы тот же экран — Telegram показал бы
    # «Назад ничего не делает». В open-root-модели источник перехода лежит
    # ГЛУБЖЕ точки входа, поэтому дальнейший поиск идём от stack[:-1].
    if current_cb and (root == current_cb or (stack and stack[-1] == current_cb)):
        # Самопетля корня недопустима, НО если в стеке есть реальная запись
        # самого корня раздела ('menu:merch'), она и есть осмысленный выход
        # на уровень выше с подстраницы (категория/товар → список мерча).
        if root != "menu:main" and _find_last(stack, root) is not None \
                and root != current_cb:
            return root
        root = "menu:main"

    # Подстраница своего раздела ('merch:cat:*'/'merch:prod:*' внутри
    # мерча, 'ev:view:*' внутри событий): точка входа в неё — НЕ
    # 'menu:<section>' (это вход в раздел целиком), а сама нажатая кнопка.
    # Иначе категория товара выглядела бы «открытой из главного меню», и
    # «Назад» перескакивал бы список категорий сразу в меню.
    subpage_cb = ""
    if section in _SUBPAGE_SECTIONS and current_cb:
        head = str(current_cb).split(":")[0]
        if head == section:
            subpage_cb = str(current_cb)

    def _is_entry(e: str) -> bool:
        """Запись стека = переход В ТЕКУЩИЙ экран (не кандидат «Назад»)."""
        if e == entry_cb or (current_cb and e == current_cb):
            return True
        # Листание страниц главного меню ('menu:page:N') — перерисовка
        # того же экрана, а не переход между разделами.
        if not section and e.startswith("menu:page:"):
            return True
        # 'pet:*' — «свои» записи только для подразделов хаба питомца;
        # для прочих секций (например, корневой хаб 'menu:pet', где эти
        # кнопки и живут) они валидные точки возврата.
        return bool(section in _PET_SUB_SECTIONS and e.startswith(("pet:page:", "pet:games", "pet:arena", "pet:friends", "pet:shop", "pet:inv", "pet:style")))

    def _pick(candidates: list[str]) -> str | None:
        """Первая подходящая запись снизу вверх + коррекция через корень."""
        for j in range(len(candidates) - 1, -1, -1):
            cb = candidates[j]
            if not cb or cb.endswith(":noop") or cb == "noop":
                continue
            if _is_entry(cb):
                continue
            # Запись равна корню раздела = сама точка входа в текущий экран
            # («Статистика» открыта кнопкой «menu:stats»). Возврат туда =
            # перерисовка того же экрана = «Назад ничего не делает».
            if root != "menu:main" and cb == root:
                continue
            if _is_own_section_entry(cb, section):
                continue
            # Если корень раздела лежит ГЛУБЖЕ найденной записи, «Назад»
            # должен вести в корень раздела, а не перескакивать его сразу
            # в главное меню (Мерч → Категория: «Назад» = список мерча).
            if root != "menu:main" \
                    and _find_last(candidates[:j], root) is not None:
                return root
            return cb
        return None

    # Индекс точки входа в ТЕКУЩИЙ раздел. Три случая:
    #  a) 'menu:<section>' есть в стеке — это вход сюда (из главного меню);
    #  b) подраздел хаба питомца ('games'/'arena'/…) — точка входа лежит
    #     в стеке как 'pet:page:*' (хаб), а не 'menu:games';
    #  c) записи входа нет вовсе: экран открыт сообщением/командой либо
    #     листанием уже открытого экрана ('menu:page:N').
    # Дальше важна ориентация вершины стека:
    #  • prod-модель (middleware пишет источник ДО отрисовки): вершина =
    #    источник перехода, текущий экран в стеке не представлен →
    #    candidates = stack[entry_i + 1:] содержит вершину;
    #  • self-model (вершина = кнопка текущего экрана: перерисовка после
    #    своего коллбэка, прямые записи в тестах): эту запись скипаем —
    #    candidates = stack[entry_i:-1]; если она одна — fallback к корню.
    entry_cb = subpage_cb if subpage_cb else f"menu:{section}"
    entry_i = _find_last(stack, entry_cb)
    pet_sub_entry = False
    if entry_i is None and not subpage_cb and section in _PET_SUB_SECTIONS:
        # Точка входа в подраздел хаба питомца — НЕ 'menu:<section>'
        # (у таких секций её и нет), а одна из кнопок-переходов:
        # вкладка хаба ('pet:page:N') или прямая кнопка ('pet:games').
        # Ищем САМУЮ ВЕРХНЮЮ такую запись: она и есть последний шаг
        # «как мы сюда попали». Записи глубже — прошлые посещения тех же
        # экранов; считать входом их нельзя (иначе «Назад» зацикливается).
        pet_entry = _find_last_prefix(
            stack, ("pet:page:", "pet:games", "pet:arena", "pet:friends",
                    "pet:shop", "pet:inv", "pet:style"))
        if pet_entry is not None:
            entry_i = max(entry_i, pet_entry) if entry_i is not None else pet_entry
            pet_sub_entry = True
    # Явный указатель «мы находимся на экране, открытом по current_cb»:
    # точка входа — последняя такая запись (перерисовки/повторные входы
    # дедуплицируются, но листание может оставить несколько).
    if current_cb:
        cur_i = _find_last(stack, current_cb)
        if cur_i is not None:
            entry_i = cur_i if entry_i is None else max(entry_i, cur_i)
    top_is_current = entry_i == len(stack) - 1
    if entry_i is None:
        entry_i = len(stack) - 1

    # 1) Источник перехода — самая верхняя подходящая запись НАД точкой
    #    входа (self-запись вершины, если она есть, в candidates не
    #    попадает).
    hi = len(stack) - 1 if top_is_current else len(stack)
    back = _pick(stack[entry_i + 1:hi])
    if back is not None:
        return back
    # 1b) Open-root-модель обработчиков (мерч/события): кнопка-источник
    #     перехода К ЭКРАНУ лежит НИЖЕ точки входа, а сама точка входа —
    #     вершина стека. Пример: стек ['menu:merch', 'merch:cat:1',
    #     'merch:prod:5'] для товара: пришли из категории 'merch:cat:1'
    #     (индекс 1) — туда и ведём. Работает и для 'menu:merch' →
    #     merch:cat:N: там точка входа 'menu:merch' (0), источник ниже неё
    #     отсутствует, и этот шаг ничего не возвращает — корректно.
    if pet_sub_entry and entry_i > 0:
        back = _pick(stack[:entry_i])
        if back is not None:
            return back
    # 2) Внутренняя история раздела: у ПОДСТРАНИЦ своего корня (категория/
    #    товар мерча, карточка события) «Назад» ведёт к корню раздела —
    #    это осмысленный выход на уровень выше. Но если сам корень и есть
    #    текущий экран ('menu:merch' — список категорий), возврат «в себя»
    #    запрещён (самопетля). Для верхнеуровневых экранов («Статистика»,
    #    карточка профиля…) корень = текущий экран → «Назад» не показываем
    #    вовсе (только «🏠 Меню»), кроме случаев, когда корнем служит
    #    вкладка общего хаба ('pet:page:*'): магазин ← хаб питомца.
    is_self_root = (root == f"menu:{section}"
                    or (section == "pet" and root.startswith("pet:")))
    if section in _SUBPAGE_SECTIONS and not is_self_root \
            and root != current_cb:
        return root
    if root.startswith("pet:page:"):
        return root
    # 3) Раздел открыт листанием уже открытого экрана ('menu:page:N' при
    #    'menu:stats'): точка входа спрятана ниже — ищем источник ещё глубже.
    if entry_i > 0:
        return _pick(stack[:entry_i])
    return None


def with_nav(b: InlineKeyboardBuilder, section: str | None,
             chat_id: int | None = None,
             current_cb: str | None = None) -> InlineKeyboardBuilder:
    """Добавляет в билдер строку «⬅️ Назад» + «🏠 Меню»; «Назад» учитывает
    локальную историю переходов этого чата.

    Для ВЕРХНЕУРОВНЕВЫХ экранов («Статистика», «Топы», «Достижения»,
    «Карточка», настройки…) кнопка «Назад» не показывается вовсе: у таких
    экранов нет подразделов, единственный осмысленный выход — «🏠 Меню».
    «Назад» туда либо вёл бы в себя (кнопка «ничего не делает»), либо
    дублировал бы кнопку «Меню». Разрешаем её только когда в истории есть
    реальный внешний источник из ДРУГОГО раздела (Мерч → Статы: «Назад»
    возвращает в мерч)."""
    back_cb = _nav_back_cb(section, chat_id, current_cb=current_cb)
    if back_cb is None and not _back_suppressed(section) \
            and section not in _SUBPAGE_SECTIONS:
        # Корень раздела == текущий экран: верхнеуровневый экран без
        # подуровней — оставляем только «🏠 Меню».
        back_cb = ""
    return append_nav(b, section, back_cb)

def games_menu(chat_id: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔢 Угадай число · 🧠 помогает", callback_data="game:guess")
    b.row()
    b.button(text="✂️ Камень-ножницы-бумага", callback_data="game:rps")
    b.button(text="🃏 Двадцать одно · 🧠 помогает", callback_data="game:blackjack")
    b.adjust(1, 2)
    # current_cb='pet:games' — нажатая кнопка входа из хаба: включает режим
    # current_source, «Назад» ведёт на вкладку 'pet:page:N', а не подавляется.
    back_cb = _nav_back_cb("games", chat_id, current_cb="pet:games")
    if not back_cb or back_cb == "pet:games":
        # Стек навигации пуст (первый заход, рестарт без Redis и т.п.) —
        # гарантируем рабочий выход: явная кнопка возврата в хаб питомца.
        # Иначе на экране игр не оставалось НИ ОДНОЙ рабочей кнопки, и
        # тапы по играм выглядели как «ничего не происходит».
        b.button(text="⬅️ Назад в питомцу", callback_data="pet:page:2")
        b.row()
        b.adjust(1, 2, 1)
    else:
        append_nav(b, "games", back_cb=back_cb)
    return b.as_markup()


def inline_back_kb(section: str = "games", *,
                   chat_id: int | None = None,
                   extra_rows: list[list[tuple[str, str]]] | None = None,
                   exit_cb: str | None = None) -> InlineKeyboardMarkup:
    """Клавиатура игрового хода с ЯВНОЙ кнопкой выхода.

    Мини-игры («Угадай число», КНБ, «Двадцать одно») живут в FSM-состоянии;
    стек-навигация («⬅️ Назад») зависит от Redis и может не построиться
    (рестарт без Redis, deep-link). Поэтому в каждой игровой клавиатуре
    всегда есть «⬅️ Выйти из игры» → 'game:exit' (сбрасывает состояние и
    возвращает в меню игр). Если навигационный «Назад» построился — он
    остаётся дополнительной опцией; если нет — выход гарантирован.
    """
    b = InlineKeyboardBuilder()
    for row in (extra_rows or []):
        if row:
            b.row(*[InlineKeyboardButton(text=t, callback_data=cb)
                    for t, cb in row])
    b.row(InlineKeyboardButton(text="⬅️ Выйти из игры",
                               callback_data=exit_cb or "game:exit"))
    append_nav(b, section, back_cb=_nav_back_cb(section, chat_id))
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
    "settings": "menu:settings",  # ⚙️ Настройки (уведомления + тема)
    "manual": "manual:home",      # 📖 Гид по уходу за питомцем
    "balance": "bal:home",        # ⚙️ Баланс (админ)
}

BACK_LABEL = "⬅️ Назад"
# «Домой» — прежняя подпись кнопки выхода в главное меню. Пользователи
# привыкли именно к ней, и на экранах с кнопкой «⬅️ Назад» слово «Меню»
# читалось как «удалённый дом»: поэтому выход всегда подписан «🏠 Домой»,
# а «Назад» — только возврат на шаг назад. Никогда не убирай «Домой»
# оттуда, где добавляешь «Назад» — это два разных действия.
HOME_LABEL = "🏠 Домой"

# Секции, экраны которых — ПОДСТРАНИЦЫ своего корня (товар/категория
# мерча, карточка события): для них «Назад → корень раздела» осмыслен
# даже при пустой истории. Верхнеуровневые экраны («Статистика»,
# «Достижения», карточка профиля…) этим корнем и являются — возврат
# в себя дал бы кнопку, «которая ничего не делает».
_SUBPAGE_SECTIONS = {"merch", "events"}

# Плоские разделы: у их экранов нет многоуровневой структуры — топы и
# достижения переключаются вкладками внутри одного экрана, поэтому
# отдельная кнопка «Назад» там не нужна (достаточно «🏠 Меню»).
FLAT_SECTIONS = {"top", "ach"}


def _back_suppressed(section: str | None) -> bool:
    """Нужно ли полностью подавить кнопку «Назад» на экране раздела."""
    return section in FLAT_SECTIONS


def _nav_buttons(back_cb: str | None) -> list[InlineKeyboardButton]:
    out: list[InlineKeyboardButton] = []
    # Страховка от самопетли: callback, равный главному меню, — это кнопка
    # «🏠 Меню», отдельная кнопка «Назад → menu:main» бессмысленна.
    if back_cb == "menu:main":
        back_cb = ""
    if back_cb and back_cb != "menu:main":
        out.append(InlineKeyboardButton(text=BACK_LABEL, callback_data=back_cb))
    # ВАЖНО: «🏠 Меню» ведёт на «menu:main» — конкретный обработчик в start.py.
    # Раньше здесь был «menu:home», который никто не обрабатывал: кнопки
    # «Меню» и «Назад» на экранах второго уровня попадали в catch-all
    # events.menu_any_unhandled («Кнопка устарела») — при этом перерисовка
    # шла в НОВОМ контексте asyncio.Task (aiogram error-handler), где тема
    # терялась: пользователь видел «тема применяется только к настройкам».
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


def nav_row(section: str | None, back_cb: str | None = None,
            chat_id: int | None = None,
            current_cb: str | None = None) -> list[InlineKeyboardButton]:
    """Готовая нижняя строка навигации для ручной сборки клавиатуры.
    back_cb=None — умный выбор по стеку навигации этого чата (если стек
    недоступен — корень раздела); back_cb="" — подавить кнопку «Назад»
    (только «Меню»). current_cb — callback, по которому открыт текущий
    экран (защита от самопетли в open-root-модели мерча/событий)."""
    if _back_suppressed(section):
        # Плоский раздел — без кнопки «Назад», только «🏠 Домой».
        return _nav_buttons("")
    root = SECTION_ROOTS.get(section or "", "menu:main")
    if back_cb is None:
        back_cb = _nav_back_cb(section, chat_id, current_cb=current_cb)
        if back_cb is None:
            # Пустой/непригодный стек. Корень раздела (для подразделов
            # хаба питомца это вкладка 'pet:page:N') — осмысленный выход
            # НА УРОВЕНЬ ВЫШЕ, показываем его даже при пустой истории:
            # с устройства пользователь мог попасть сюда deep-link'ом или
            # после рестарта без Redis. Для верхнеуровневых экранов
            # («Статистика», карточка…) корень == текущий экран → возврат
            # «в себя» бессмыслен, оставляем только «🏠 Домой».
            pet_sub_root = section in _PET_SUB_SECTIONS and \
                root.startswith("pet:page:")
            is_subpage = section in _SUBPAGE_SECTIONS and \
                str(current_cb or "").split(":")[0] == section
            back_cb = root if (is_subpage or pet_sub_root) else ""
    # Самопетля недопустима: «Назад» на тот же callback, по которому мы
    # сейчас находимся, = Telegram покажет «ничего не происходит».
    # Корень раздела ('menu:merch' для списка категорий) блокируется ТОЛЬКО
    # когда он и есть текущий экран: список категорий мерча открыт кнопкой
    # 'menu:merch' → возврат туда = перерисовка того же экрана. С подстраниц
    # (товар/категория/позиция) 'menu:merch' — валидный пункт возврата на
    # уровень выше, и блокировать его нельзя (иначе «Назад» исчезает совсем).
    explicit = back_cb is not None and back_cb != ""
    # «Назад» ведёт на вкладку хаба питомца ('pet:page:N'): эта вкладка
    # живёт ВНУТРИ сообщения-хаба, но НЕ внутри текущего экрана — это
    # реальный переход на уровень выше (Стиль → вкладка «🎒 Вещи»), его
    # блокировать нельзя. Раньше этот случай ошибочно считался
    # самопетлёй, и кнопка «Назад» исчезала из подразделов хаба
    # (стиль/магазин/инвентарь/игры) — оставался только «Домой».
    pet_tab_back = str(back_cb or "").startswith("pet:page:")
    if not explicit:
        on_root_screen = (not current_cb) or current_cb == root \
            or str(current_cb).split(":")[0] == section
        if back_cb and back_cb == root and on_root_screen and not pet_tab_back:
            back_cb = ""
    elif back_cb == root and root != f"menu:{section}":
        # Подраздел хаба (магазин/игры ← вкладка 'pet:page:N'): корень
        # раздела лежит ВНУТРИ того же экрана-сообщения, и возврат туда =
        # перерисовка текущего сообщения («Назад ничего не делает»).
        # Такую явную цель тоже блокируем — остаётся «🏠 Домой».
        # Исключение — сам возврат на вкладку-корень: с экрана подраздела
        # (открыт КНОПКОЙ 'pet:style'/'shop:...') переход на 'pet:page:1'
        # перерисует хаб — это осмысленный выход наружу, а не самопетля.
        sub_from_hub = section in _PET_SUB_SECTIONS and \
            str(current_cb or "").startswith(("pet:", "shop", "inv", "style"))
        if not (sub_from_hub and pet_tab_back):
            back_cb = ""
    if back_cb and back_cb == current_cb:
        back_cb = ""
    return _nav_buttons(back_cb)


def append_nav(b: InlineKeyboardBuilder, section: str | None,
               back_cb: str | None = None,
               chat_id: int | None = None,
               current_cb: str | None = None) -> InlineKeyboardBuilder:
    """Добавляет в билдер нижнюю строку «⬅️ Назад» + «🏠 Меню»."""
    b.row(*nav_row(section, back_cb, chat_id, current_cb))
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
    b.button(text=HOME_LABEL, callback_data="menu:main")
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

def theme_picker_keyboard(current_key: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора темы оформления (экран «Настройки»)."""
    from app import themes

    b = InlineKeyboardBuilder()
    for th in themes.available_themes():
        mark = "✅" if th.key == current_key else "▫️"
        preview = " ".join(th.preview_emoji)
        b.button(text=f"{mark} {preview} {th.title.split(' ', 1)[-1]}",
                 callback_data=f"set:theme:{th.key}")
    b.adjust(1)
    return b.as_markup(one_time=False)


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
    # current_cb='pet:style' активирует режим current_source в _nav_back_cb:
    # источник перехода ищется ГЛУБЖЕ вершины стека (вершина == сама кнопка
    # «Стиль», положенная middleware до отрисовки) → «Назад» ведёт на ту
    # вкладку/экран, откуда реально пришли, а не в корень раздела.
    append_nav(b, "style", back_cb=_nav_back_cb("style", chat_id,
                                                current_cb="pet:style"))
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


# ============================================================================
# Погодный раздел (wthr:*) и клавиатура дневного отчёта
# ============================================================================

def weather_kb(view: str = "today") -> _IKM:
    """Навигация погодного экрана: «Сегодня» / «Неделя» + возврат в меню.

    Активный таб подсвечивается галочкой ✅ — экран двухрежимный, и без
    метки пользователь не понимает, где он сейчас.
    """
    b = InlineKeyboardBuilder()
    b.button(text=("✅ Сегодня" if view == "today" else "📅 Сегодня"),
             callback_data="wthr:today")
    b.button(text=("✅ Неделя" if view == "week" else "🗓 Неделя"),
             callback_data="wthr:week")
    # Кнопки «🔄 Обновить» здесь намеренно НЕТ: обновление погоды выполняет
    # серверный планировщик (job weather_updater). Ручные refresh от каждого
    # пользователя создавали спам запросов к OpenWeather и риск блокировки
    # API-ключа по лимиту.
    b.adjust(2)
    # «menu:main» — единственный обработчик возврата в главное меню
    # (голого payload «menu» в боте нет — такая кнопка была мёртвой).
    b.row(_IKB(text=HOME_LABEL, callback_data="menu:main"))
    return b.as_markup()


def daily_report_kb() -> _IKM:
    """Кнопки под вечерним дневным отчётом («Твой день»).

    Без этой клавиатуры фраза «Жми „Продолжить день“» в тексте отчёта
    ведёт в никуда — кнопка обязательна.
    """
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Продолжить день", callback_data="menu:main")
    b.button(text="🐾 Питомец", callback_data="menu:pet")
    b.adjust(1)
    return b.as_markup()
