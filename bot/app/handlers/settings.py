from __future__ import annotations

import contextlib

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Chat, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import themes
from app.db.models import User
from app.db.repositories import NotificationRepository, UserRepository
from app.keyboards.inline import back_to_main, settings_keyboard, theme_picker_keyboard
from app.services.leaderboard import leaderboard_text, snapshot_weekly
from app.utils.safe_edit import answer_safe, safe_edit_or_answer

router = Router(name="settings")

_FLAG_LABELS = {
    "pet_reminders": "🐾 Питомец скучает",
    "streak_reminders": "🔥 Стрик под угрозой",
    "achievement_notifications": "🏆 Достижения",
    "daily_report": "🌅 Ежедневный отчёт",
}


def _theme_intro() -> str:
    theme = themes.THEMES.get(themes.current_theme_key())
    if theme and theme.settings_intro:
        return theme.settings_intro
    return ("⚙️ <b>Настройки</b>\n\n"
            "Я пишу в ЛС только когда это действительно нужно.\n"
            "Здесь можно всё отключить — нажми на тумблер:\n\n")


async def _render_settings(session: AsyncSession, message: Message, tg_id: int,
                           chat_id: int | None = None) -> None:
    # на всякий случай актуализируем тему из БД (если middleware не отработал)
    db_user0 = await session.get(User, tg_id)
    key0 = ((db_user0.settings_extra or {}).get("theme")
            if db_user0 else None) or themes.DEFAULT_THEME_KEY
    themes.set_theme(key0)
    ns = await NotificationRepository(session).get_or_create(tg_id)
    flags = {k: bool(getattr(ns, k)) for k in _FLAG_LABELS}
    text = (_theme_intro()
            + "\n".join(f"{'✅' if flags[k] else '❌'} {themes.gothic(label)}"
                        for k, label in _FLAG_LABELS.items()))
    # Блок выбора темы оформления
    cur = themes.theme_for_key(key0)
    text += ("\n\n🎭 <b>Тема оформления</b>\n"
             f"Сейчас: <b>{cur.title}</b> — {cur.tagline}\n"
             "Выбери другую:")
    kb = settings_keyboard(flags, chat_id)
    theme_rows = theme_picker_keyboard(cur.key).inline_keyboard
    kb.inline_keyboard = theme_rows + list(kb.inline_keyboard)
    await safe_edit_or_answer(message, text, reply_markup=kb)


class _HistoryMsg:
    """Адаптер Telethon-сообщения под интерфейс aiogram Message (для edit_text)."""

    def __init__(self, bot: Bot, m) -> None:
        self._bot = bot
        self._m = m
        self.message_id = m.id
        self.chat = Chat(id=int(m.chat_id), type="private")

    async def edit_text(self, text: str, *, reply_markup=None,
                        parse_mode: str | None = "HTML", **_kw):
        await self._bot.edit_message_text(chat_id=self.chat.id,
                                          message_id=self.message_id,
                                          text=text, parse_mode=parse_mode,
                                          reply_markup=reply_markup)
        return self

    async def answer(self, text: str, **kwargs):  # pragma: no cover
        return None

@router.callback_query(F.data == "menu:settings")
async def cb_settings(cb: CallbackQuery, session: AsyncSession) -> None:
    # Принудительно ставим тему пользователя в контекст ЭТОЙ задачи.
    # Обычно это делает ThemeMiddleware, но если апдейт каким-то путём его
    # обошёл (новый процесс, сбой чтения БД), экран настроек обязан быть
    # в выбранной теме — иначе пользователь видит «тема не применяется».
    themes.set_theme(await themes.load_theme_key(cb.from_user.id)
                     or themes.current_theme_key())
    await _render_settings(session, cb.message, cb.from_user.id,
                           chat_id=cb.message.chat.id if cb.message else None)
    await cb.answer()


# Совместимость со СТАРЫМИ сообщениями настроек: в них кнопка «🏠 Меню»
# имела callback «menu:home» (навигация давно отдаёт «menu:main»). Тап по
# старой кнопке уходил в catch-all «Кнопка устарела», чья перерисовка шла
# в новом контексте asyncio.Task без темы — отсюда ощущение «тема работает
# только в настройках». start.py уже обрабатывает «menu:home» как главное
# меню; здесь дополнительно подстраховываемся: если в чате остались и
# старые экраны настроек с такой кнопкой, нажатие вернёт именно их
# (узнаём по соседним «set:*» в клавиатуре сообщения).
@router.callback_query(F.data == "menu:home")
async def cb_settings_home_alias(cb: CallbackQuery, session: AsyncSession) -> None:
    kb = getattr(cb.message, "reply_markup", None) if cb.message else None
    rows = getattr(kb, "inline_keyboard", None) or []
    has_set = any((btn.callback_data or "").startswith("set:")
                  for row in rows for btn in row)
    if not has_set:
        return  # обычный случай — пусть работает алиас главного меню из start.py
    await cb_settings(cb, session)

# ── Мгновенная перекраска уже показанных сообщений ────────────────────────
# Тема применяется ко всем НОВЫМ сообщениям автоматически (ThemeMiddleware +
# тематизированные кнопки), но старые сообщения в чате остаются в прежнем
# стиле. При смене темы мы проходим по последним сообщениям бота в ЛС и
# перерисовываем те из них, где под кнопками спрятан главный экран или
# экран настроек (callback_data «menu:*» / «set:*»).
#
# Важно: Bot.get_chat_history — это метод MTProto-клиента (Pyrogram), у
# aiogram-овского Bot его нет. Поэтому историю читаем через MTProto-клиент
# (тот же механизм, что листенер реакций) и редактируем через обычный Bot API.

_THEMEABLE_BTN_PREFIXES = ("menu:", "set:")


def _main_menu_text_from_buttons(kb) -> str | None:
    """Восстанавливает СТАНДАРТНЫЙ текст главного меню по кнопкам сообщения.

    Заголовок страницы берём из подписи «menu:noop» («🎮 Игра 📖 1/2», в
    готике — «🃏 Игра …»), сопоставляя её с любой страницей MENU_PAGES по
    последнему слову (эмодзи при этом не важен). Остальной текст — канонический
    стандартный, поверх него перекраска таблицей эмодзи работает корректно и
    идемпотентно (можно перекрашивать уже перекрашенное сообщение).
    """
    from app.keyboards.inline import MENU_PAGES
    label = ""
    for row in kb.inline_keyboard:
        for btn in row:
            if (btn.callback_data or "") == "menu:noop" and (btn.text or "").strip():
                label = btn.text.strip()
    if not label:
        return None
    core = label.split("📖")[0].strip()
    last_word = core.split()[-1] if core.split() else ""
    title = None
    for t, _a in MENU_PAGES:
        if t.split()[-1] == last_word:
            title = t
            break
    if title is None:
        return None
    from app import themes
    # Канон текста — в themes.standard_main_menu_std(); здесь только восстановление
    # заголовка по кнопке «menu:noop». Раньше список «Что делать» был скопирован
    # вручную и устаревал при изменении меню (мерч убран, добавлена строка
    # питомца) — при перекраске старых сообщений пользователь видел прошлый ликбез.
    text = themes.standard_main_menu_std().replace("{title}", title)
    # standard_main_menu_std уже содержит заголовок/строки показателей/ликбез/
    # строку канала. Маркер строки питомца {pet} убираем: при перекраске у нас
    # нет данных о питомце, а живой рендер (/start, «🏠 Домой») подставит его
    # сам. Оставлять "{pet}" нельзя — он дожил бы до пользователя как сырой
    # шаблон (main_menu_renders подставляет {pet} только на живом рендере).
    text = "\n".join(ln for ln in text.splitlines() if ln.strip() != "{pet}")
    # Маркеры показателей («👤 {name}, уровень {level} …», «🪙 Монеты: {coins}
    # …») заменяем сохранённой строкой статистики из старого сообщения: дампа
    # отдельных значений нет, но исходный текст «уровень/XP/монеты» мы видим.
    stats_line = _extract_stats_line(text)
    if stats_line is not None:
        text = "\n".join(
            ln for ln in text.splitlines()
            if not ln.startswith("👤 {name},") and not ln.startswith("🪙 Монеты:")
        )
        text = text.replace("\n\n\n", "\n\n")  # не плодить пустые строки
        marker_pos = text.find("🐾 Твой питомец:")
        if marker_pos != -1:
            head = text[:marker_pos].rstrip("\n")
            text = f"{head}\n{stats_line}\n\n{text[marker_pos:]}"
    # Маркер приветствия {name}: имя автора сообщения недоступно (в дампе
    # клавиатуры его нет), поэтому подставляем нейтральное обращение — иначе
    # сырой "{name}" дожил бы до пользователя. В сохранённой строке
    # статистики имя тоже неизвестно — там остаётся «друг».
    text = text.replace("{name}", "друг")
    return text


def _extract_stats_line(text: str) -> str | None:
    """Вытаскивает из старого текста меню строку «👤 … XP» (+ «🪙 …»).

    Возвращает восстановленную стандартную пару строк показателей или None,
    если сообщение слишком старое/битое (тогда маркеры шаблона чищаются
    заменой {name} и пользователь увидит меню без показателей, но без
    сырых «{level}»).
    """
    import re
    m_lvl = re.search(r"Уровень\s+(\d+).*?(\d+)/(\d+)\s*XP", text)
    m_coin = re.search(r"(?:Монеты|Капли крови)[:\s]*(\d+)", text)
    m_streak = re.search(r"(?:Серия|Свеча горит)[:\s]*(\d+)\s*дн", text)
    if not (m_lvl and m_coin):
        return None
    bar_m = re.search(r"[▰▱]{10}", text)
    bar = bar_m.group(0) if bar_m else ""
    lvl, xp, need = m_lvl.group(1), m_lvl.group(2), m_lvl.group(3)
    streak = m_streak.group(1) if m_streak else "0"
    return (f"👤 друг, уровень {lvl} · {bar} {xp}/{need} XP\n"
            f"🪙 Монеты: {m_coin.group(1)} · 🔥 Серия: {streak} дн.")


def _detect_screen(cbs: list[str]) -> str | None:
    """Экран сообщения по callback_data его кнопок."""
    if any(c == "menu:settings" or c.startswith("set:") for c in cbs):
        return "settings"
    if any(c.startswith("menu:") for c in cbs):
        return "main"
    if any(c.startswith("back:") or c == "home" for c in cbs):
        return "sub"  # вложенные экраны: перекрашиваем только текст
    return None


async def _get_mtproto():
    try:
        from app.services.mtproto_client import holder
        client = await holder.get()
    except Exception as exc:
        logger.debug(f"repaint: MTProto недоступен ({exc})")
        return None
    if client is None or not getattr(holder, "is_connected", False):
        return None
    return client


async def _fetch_history(client, chat_id: int, limit: int) -> list:
    """Последние сообщения чата через MTProto (aiogram-бот историю не отдаёт)."""
    peer = chat_id
    if chat_id < 0:  # -100... — supergroup/channel
        with contextlib.suppress(Exception):
            peer = int(str(chat_id)[4:])
    msgs = []
    async for m in client.iter_messages(peer, limit=limit):
        if m is not None:
            msgs.append(m)
    return msgs


def _telethon_markup_to_aiogram(kb):
    """Telethon ReplyKeyboardMarkup (inline) -> InlineKeyboardMarkup aiogram."""
    from aiogram.types import InlineKeyboardMarkup

    from app.keyboards.inline import InlineKeyboardButton
    rows = []
    for row in kb.rows:
        r = []
        for b in row:
            text = getattr(b, "text", "") or ""
            url = getattr(b, "url", None) or getattr(b, "button_url", None)
            data = getattr(b, "data", None)
            if isinstance(data, bytes):
                data = data.decode("utf-8", "replace")
            r.append(InlineKeyboardButton(text=text,
                                          callback_data=data, url=url))
        rows.append(r)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _retheme_message_text(text: str, new_theme_key: str) -> str | None:
    """Перекрашивает текст сообщения под новую тему.

    В готику — прямая эмодзи-таблица (идемпотентна: повторная перекраска
    уже готического текста ничего не меняет); обратно в стандарт — ничего
    не делаем (восстановление исходного текста по готической разметке
    ненадёжно, а после /start экраны перерисовываются в стандарте целиком).
    """
    if new_theme_key == themes.STANDARD.key:
        return None
    mapped = themes._map_emoji(text, themes.GOTHIC.emoji_map)
    return mapped if mapped != text else None


async def repaint_main_menu(bot: Bot, chat_id: int, theme_key: str) -> int:
    """Перерисовывает сообщение главного меню в ЛС под новую тему.

    Главное меню может быть открыто на любой странице — опознаём его по
    кнопке-заглушке «menu:noop» (её подпись не тематизируется и служит
    маркером). Это единственный экран, который нужно перезарисовать сразу
    при смене темы: он висит над чатом как постоянная точка входа.
    """
    client = await _get_mtproto()
    if client is None:
        return 0
    try:
        me = await bot.get_me()
        msgs = await _fetch_history(client, chat_id, 40)
    except Exception as exc:
        logger.debug(f"repaint main menu: history unavailable ({exc})")
        return 0
    repainted = 0
    for m in msgs:
        from_user = getattr(m, "from_user", None)
        if from_user is None or from_user.id != me.id:
            continue
        text = getattr(m, "text", None) or getattr(m, "caption", None)
        kb_src = getattr(m, "reply_markup", None)
        if not text or kb_src is None:
            continue
        try:
            kb = _telethon_markup_to_aiogram(kb_src)
        except Exception as exc:
            logger.debug(f"telethon markup conversion failed for "
                         f"{getattr(m, 'id', '?')}: {exc!r}")
            continue
        cbs = [btn.callback_data or ""
               for row in kb.inline_keyboard for btn in row]
        if _detect_screen(cbs) != "main":
            continue
        std = _main_menu_text_from_buttons(kb)
        if not std:
            continue
        stats_line = ""
        for ln in text.splitlines():
            if ("уровень" in ln or "ступень" in ln) and "XP" in ln:
                stats_line = ln
                break
        std = std.replace("{stats}", stats_line or " ")
        new_text = _retheme_message_text(std, theme_key)
        if not new_text:
            continue
        kb = _rebuild_kb(std, kb, theme_key)
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=m.message_id,
                                         text=new_text, parse_mode="HTML",
                                         reply_markup=kb)
            repainted += 1
        except TelegramAPIError:
            continue
    return repainted


async def repaint_chat_messages(bot: Bot, chat_id: int, theme_key: str,
                                limit: int = 40) -> int:
    """Перерисовывает последние сообщения бота в чате под новую тему.

    Определяет экран по callback_data кнопок сообщения:
      • настройки («menu:settings» / «set:*») -> перекрашиваем весь текст;
      • главное меню («menu:*») -> восстанавливаем стандартный текст по
        подписям кнопок, перекрашиваем и пересобираем клавиатуру в новой теме;
      • прочие экраны с навигацией -> перекрашиваем текст как есть.
    """
    repainted = 0
    client = await _get_mtproto()
    if client is None:
        return 0
    try:
        me = await bot.get_me()
        msgs = await _fetch_history(client, chat_id, limit)
    except TelegramAPIError as exc:
        logger.debug(f"repaint: history unavailable ({exc})")
        return 0
    except Exception as exc:
        logger.debug(f"repaint: mtproto history failed: {exc}")
        return 0
    for m in msgs:
        from_user = getattr(m, "from_user", None)
        if from_user is None or from_user.id != me.id:
            continue
        text = getattr(m, "text", None) or getattr(m, "caption", None)
        kb_src = getattr(m, "reply_markup", None)
        if not text or kb_src is None:
            continue
        try:
            kb = _telethon_markup_to_aiogram(kb_src)
        except Exception as exc:
            logger.debug(f"telethon markup conversion failed for "
                         f"{getattr(m, 'id', '?')}: {exc!r}")
            continue
        cbs = [btn.callback_data or ""
               for row in kb.inline_keyboard for btn in row]
        screen = _detect_screen(cbs)
        if screen is None:
            continue
        new_text: str | None = None
        if screen == "main":
            std = _main_menu_text_from_buttons(kb)
            if std:
                # подставляем сохранённые показатели из строки со статистикой
                stats_line = ""
                for ln in text.splitlines():
                    if ("уровень" in ln or "ступень" in ln) and "XP" in ln:
                        stats_line = ln
                        break
                std = std.replace("{stats}", stats_line or " ")
                new_text = _retheme_message_text(std, theme_key)
                if new_text:
                    kb = _rebuild_kb(std, kb, theme_key)
        elif screen == "settings":
            # экран настроек перерисовываем ЦЕЛИКОМ (тумблеры + блок темы с
            # актуальным списком тем), а не эмодзи-таблицей — иначе готика
            # выглядела бы «недостаточно готической»
            try:
                from app.db.session import session_factory

                hm = _HistoryMsg(bot, m)
                async with session_factory() as s_:
                    await _render_settings(s_, hm, chat_id, chat_id=chat_id)
                repainted += 1
                continue
            except Exception as exc:
                logger.debug(f"repaint: settings re-render failed: {exc}")
                new_text = _retheme_message_text(text, theme_key)
        else:  # sub
            new_text = _retheme_message_text(text, theme_key)
        if new_text is None or new_text == text:
            continue
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=m.message_id,
                                         text=new_text, parse_mode="HTML",
                                         reply_markup=kb)
            repainted += 1
        except TelegramAPIError:
            continue
    return repainted


def _rebuild_kb(std_text: str, kb, theme_key: str):
    """Пересобирает клавиатуру главного меню в новой теме.

    callback_data кнопок берём из СТАРОЙ клавиатуры сообщения (они не
    меняются вместе с темой), а подписи — из восстановленного стандартного
    текста («• <эмодзи> <подпись> …»). При создании новых кнопок активна
    целевая тема, поэтому ThemeButton.__init__ сам подставит готические
    подписи по callback_data.
    """
    from app.keyboards.inline import MENU_PAGES, InlineKeyboardButton, _page_nav
    labels: list[str] = []
    for ln in std_text.splitlines():
        s = ln.strip()
        if s.startswith("•"):
            head = s[1:].strip()
            # «🐾 Питомец — покорми...» / «🌦️ Загляни в /weather — ...» /
            # «⚔️ Попробуй Арену — ...»: подпись = до тире, но не короче
            # первых двух слов (чтобы не резать «Ночные службы», «Мои настройки»)
            seg = head.split("—")[0].split("(")[0].strip()
            if "—" in head:
                words = seg.split()
                if len(words) >= 2:
                    seg = " ".join(words[:2])
            labels.append(seg)
    old_rows = [[btn for btn in row
                 if (btn.callback_data or "").startswith("menu:")
                 and not (btn.callback_data or "").startswith(("menu:page:", "menu:noop"))]
                for row in kb.inline_keyboard]
    old_rows = [r for r in old_rows if r]
    nav_title = ""
    for row in kb.inline_keyboard:
        for btn in row:
            if (btn.callback_data or "").startswith("menu:page:") and "\U0001F4D6" in (btn.text or ""):
                nav_title = btn.text.split("\U0001F4D6")[0].strip()
    pages = list(MENU_PAGES)
    page_idx = next((i for i, (t, _a) in enumerate(pages)
                     if t.strip() == nav_title), 0)
    rows: list[list] = []
    it = iter(labels)
    old_flat = [btn for r in old_rows for btn in r]
    # Живая метка погоды со СТАРОЙ кнопки — после пересборки темы подпись
    # «menu:weather» всегда берётся из кэша погоды, а не из текста меню.
    weather_lbl = next((b.text for b in old_flat
                        if (b.callback_data or "") == "menu:weather"), None)
    # создаём кнопки при активной целевой теме — ThemeButton.__init__
    # подставит тематические подписи по callback_data
    prev_theme = themes.current_theme_key()
    themes.set_theme(theme_key)
    try:
        for old_btn in old_flat:
            lbl = next(it, None)
            if lbl is None:
                break
            cb = old_btn.callback_data or ""
            if cb == "menu:weather" and weather_lbl:
                lbl = weather_lbl  # не теряем живую температуру на кнопке
            if not rows or len(rows[-1]) >= 2:
                rows.append([])
            rows[-1].append(InlineKeyboardButton(text=lbl, callback_data=cb))
        if rows:
            rows.append(_page_nav("menu", page_idx, len(pages),
                                  pages[page_idx][0]))
    finally:
        themes.set_theme(prev_theme)
    if not rows:
        return kb
    from aiogram.types import InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("set:theme:"))
async def cb_set_theme(cb: CallbackQuery, session: AsyncSession,
                       bot: Bot) -> None:
    key = cb.data.split(":", 2)[2]
    if key not in themes.THEMES:
        await cb.answer("Неизвестная тема", show_alert=True)
        return
    user = await UserRepository(session).get_or_create(
        cb.from_user.id, cb.from_user.first_name or "", cb.from_user.username)
    extra = dict(user.settings_extra or {})
    extra["theme"] = key
    user.settings_extra = extra
    flag_modified(user, "settings_extra")
    await session.commit()
    themes.invalidate_theme_cache(cb.from_user.id)  # новый выбор темы — сбрасываем кэш процесса
    # СРАЗУ прогреваем процессный кэш новым значением. Без этого шага
    # ThemeGuardMiddleware (inner, перед каждым хендлером) при следующем
    # апдейте читает ПУСТОЙ кэш и оставляет контекст как есть — а aiogram
    # запускает каждую задачу апдейта в НОВОМ asyncio-контексте, где
    # contextvar темы = default(standard). Именно так готика «слетала» на
    # любом экране, кроме самих настроек: outer-слой ставил тему из БД, но
    # guard тут же ничего не находил в кэше и не переключал её обратно.
    themes.forget_and_remember(cb.from_user.id, key)
    themes.set_theme(key)  # сразу перекрашиваем ответ и последующие экраны
    th = themes.theme_for_key(key)
    chat_id = cb.message.chat.id if cb.message else None
    await _render_settings(session, cb.message, cb.from_user.id,
                           chat_id=chat_id)
    # Перезарисовываем ГЛАВНОЕ МЕНЮ в ЛС: у него может быть открыта любая
    # страница (menu:noop «🎮 Игра 📖 1/2»), а без этого шага пользователь
    # видел бы готику только на экране настроек до первого своего тапа.
    n_repainted = 0
    if chat_id is not None:
        try:
            n_repainted += await repaint_main_menu(bot, chat_id, key)
        except Exception as exc:
            logger.debug(f"theme main-menu repaint failed: {exc}")
    # перекрашиваем старые сообщения, чтобы тема было видно СРАЗУ
    if chat_id is not None:
        try:
            n_repainted += await repaint_chat_messages(bot, chat_id, key)
        except Exception as exc:
            logger.debug(f"theme repaint failed: {exc}")
    toast = f"Тема изменена: {th.title}"
    if n_repainted:
        toast += f" · обновлено сообщений: {n_repainted}"
    await cb.answer(toast[:200])


@router.callback_query(F.data.startswith("set:"))
async def cb_toggle(cb: CallbackQuery, session: AsyncSession) -> None:
    key = cb.data.split(":", 1)[1]
    if key not in _FLAG_LABELS:
        await cb.answer("Неизвестная настройка", show_alert=True)
        return
    repo = NotificationRepository(session)
    ns = await repo.get_or_create(cb.from_user.id)
    new = not bool(getattr(ns, key))
    setattr(ns, key, new)
    await session.commit()
    await _render_settings(session, cb.message, cb.from_user.id,
                           chat_id=cb.message.chat.id if cb.message else None)
    await cb.answer(("Включено: " if new else "Выключено: ") + _FLAG_LABELS[key])

@router.message(Command("award", "awards"), F.chat.type == "private")
async def cmd_awards(message: Message, session: AsyncSession) -> None:
    payload = await snapshot_weekly(session)
    if not payload:
        await message.answer("Пока нечего показать — топ будет после первой недели 🏁")
        return
    await answer_safe(message, leaderboard_text(payload),
                      reply_markup=back_to_main())

@router.message(Command("settings"), F.chat.type == "private")
async def cmd_settings(message: Message, session: AsyncSession) -> None:
    await _render_settings(session, message, message.from_user.id)



