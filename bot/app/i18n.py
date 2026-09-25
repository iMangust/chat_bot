"""i18n-слой (заложены принципы UX: «локализация — русский по умолчанию»).

Механика:
  * контекстная переменная ``_current_lang`` выставляется middleware'ом
    ``UserLanguageMiddleware`` на каждый апдейт из ``users.lang``;
  * функции перевода: ``t(key, **kwargs)`` / ``tf(lang, key, **kwargs)``;
  * при отсутствии строки или языка — молча откатываемся на RU (дефолт),
    а при полностью отсутствующем ключе — сам ключ (чтобы UI не падал).

Новые языки добавляются отдельным словарём в ``CATALOGS`` + пунктом в
``SUPPORTED_LANGS`` — разбросанных ``if lang == ...`` по хендлерам нет.
"""
from __future__ import annotations

import contextvars
from typing import Any

DEFAULT_LANG = "ru"
SUPPORTED_LANGS = ("ru", "en")

_current_lang: contextvars.ContextVar[str] = contextvars.ContextVar(
    "bot_lang", default=DEFAULT_LANG
)


def set_current_lang(lang: str | None) -> None:
    """Вызывается middleware'ом; неизвестный язык => дефолтный."""
    _current_lang.set((lang or DEFAULT_LANG).lower()[:2]
                      if (lang or "").lower()[:2] in SUPPORTED_LANGS else DEFAULT_LANG)


def get_current_lang() -> str:
    return _current_lang.get()


# ---------------------------------------------------------------------------
# Каталоги. Ключи — доменные (pet.*, shop.*, stats.*); {} — плейсхолдеры str.format.
# ---------------------------------------------------------------------------
_CATALOG_RU: dict[str, str] = {
    # --- питомец: действия ---
    "pet.eaten": "🍎 Ням-ням{tail}",
    "pet.sleeping_deny": "😴 Питомец спит — не мешай!",
    "pet.too_tired_play": "😩 Питомец слишком устал для игр. Пусть поспит!",
    "pet.won_game": "🎉 Победа! Питомец в восторге! +{xp} XP",
    "pet.lost_game": "🙂 Не повезло, но питомцу всё равно весело. +{xp} XP",
    "pet.already_sleeping": "😴 Он уже спит.",
    "pet.fell_asleep": "💤 Питомец уснул до {time} UTC. Энергия восстановится.",
    "pet.washed": "🛁 Чистенький и пахучий! Гигиена +40",
    "pet.healed": "💊 Лечение помогло! Здоровье +35",
    "pet.not_sick": "😀 Питомец здоров, лекарство не нужно.",
    "pet.walk_started": "🚶 Питомец ушёл гулять на {hours} ч. Вернётся с новостями!",
    "pet.walk_already": "🚶 Питомец уже гуляет, вернётся через ~{minutes} мин.",
    "pet.train_done": "🏋️ Тренировка завершена! {label} +{gain}",
    "pet.train_stat": "💪 Сила", "pet.train_agi": "🏃 Ловкость", "pet.train_int": "🧠 Интеллект",
    "pet.cooldown_feed": "⏳ Питомец только что ел! Подожди {sec} сек.",
    "pet.cooldown_generic": "⏳ Перерыв между действиями: {sec} сек.",
    "pet.mood_great": "Великолепно!",
    "pet.mood_good": "Хорошее настроение",
    "pet.mood_ok": "Нормально",
    "pet.mood_sad": "Грустит… удели внимание",
    "pet.mood_sick": "Больной! Нужно лечение 💊",
    "pet.mood_sleeping": "Спит… не буди 💤",
    "pet.mood_hungry": "Голодный! Дай поесть 🍎",
    # --- топы/статы ---
    "top.title": "🏅 <b>Топы чата · {period}</b>",
    "top.talkers": "💬 <b>Болтуны</b>",
    "top.reactors": "💖 <b>По полученным реакциям</b>",
    "top.streaks": "🔥 <b>Серии дней</b>",
    "top.pets": "🐾 <b>Питомцы</b>",
    "top.levels": "🏅 <b>Уровни игроков</b>",
    "top.empty": "   пока пусто — будь первым! 💬",
    "top.me_hint": " 👈 <i>это ты</i>",
    "top.award_hint": "<i>/award — итоги прошлой недели с призами 🎁</i>",
    "stats.title": "📊 <b>Твоя статистика</b>",
    "ach.title": "🏆 <b>Достижения</b>",
    # --- магазин ---
    "shop.title": "🛒 <b>Магазин питомца</b> · у тебя 🪙 {coins}",
    "shop.bought": "Куплено: {icon} {name}!",
    "shop.no_pet": "Нет питомца или пользователя",
    "shop.not_enough": "Не хватает {missing} монет 🪙",
    "inv.empty": "🎒 Инвентарь пуст. Загляни в 🛒 Магазин!",
    # --- уведомления ---
    "notif.pet_sad": "🐾 {name} {reason}",
    "notif.streak_burned": "🔥 Твоя серия дней сгорела. Начни новую — напиши что-нибудь в чат!",
    "notif.weekly_prize": "🎉 Ты #{place} в недельном топе болтунов! Приз: 🪙 {prize} монет.",
    # --- misc ---
    "common.no_pet": "🥚 У тебя пока нет питомца. Нажми /start и пройди онбординг!",
    "common.back": "⬅️ Назад",
}

_CATALOG_EN: dict[str, str] = {
    "pet.eaten": "🍎 Yum-yum{tail}",
    "pet.sleeping_deny": "😴 The pet is sleeping — don't disturb!",
    "pet.too_tired_play": "😩 Too tired for games. Let it sleep!",
    "pet.won_game": "🎉 Victory! The pet is thrilled! +{xp} XP",
    "pet.lost_game": "🙂 No luck, but the pet had fun anyway. +{xp} XP",
    "pet.already_sleeping": "😴 It's already asleep.",
    "pet.fell_asleep": "💤 Fell asleep until {time} UTC. Energy will recover.",
    "pet.washed": "🛁 Clean and fragrant! Hygiene +40",
    "pet.healed": "💊 Treatment helped! Health +35",
    "pet.not_sick": "😀 The pet is healthy, no medicine needed.",
    "pet.walk_started": "🚶 Off for a {hours} h walk. Back with news!",
    "pet.walk_already": "🚶 Already walking, back in ~{minutes} min.",
    "pet.train_done": "🏋️ Training done! {label} +{gain}",
    "pet.train_stat": "💪 Strength", "pet.train_agi": "🏃 Agility", "pet.train_int": "🧠 Intellect",
    "pet.cooldown_feed": "⏳ Just ate! Wait {sec} sec.",
    "pet.cooldown_generic": "⏳ Cooldown: {sec} sec.",
    "pet.mood_great": "Awesome!",
    "pet.mood_good": "In a good mood",
    "pet.mood_ok": "Fine",
    "pet.mood_sad": "Sad… needs attention",
    "pet.mood_sick": "Sick! Needs treatment 💊",
    "pet.mood_sleeping": "Sleeping… don't wake 💤",
    "pet.mood_hungry": "Hungry! Feed me 🍎",
    "top.title": "🏅 <b>Chat top · {period}</b>",
    "top.talkers": "💬 <b>Chatters</b>",
    "top.reactors": "💖 <b>Most reactions received</b>",
    "top.streaks": "🔥 <b>Day streaks</b>",
    "top.pets": "🐾 <b>Pets</b>",
    "top.levels": "🏅 <b>Player levels</b>",
    "top.empty": "   empty so far — be first! 💬",
    "top.me_hint": " 👈 <i>that's you</i>",
    "top.award_hint": "<i>/award — last week's results with prizes 🎁</i>",
    "stats.title": "📊 <b>Your statistics</b>",
    "ach.title": "🏆 <b>Achievements</b>",
    "shop.title": "🛒 <b>Pet shop</b> · you have 🪙 {coins}",
    "shop.bought": "Purchased: {icon} {name}!",
    "shop.no_pet": "No pet or user found",
    "shop.not_enough": "Missing {missing} coins 🪙",
    "inv.empty": "🎒 Inventory is empty. Visit the 🛒 Shop!",
    "notif.pet_sad": "🐾 {name} {reason}",
    "notif.streak_burned": "🔥 Your day streak burned out. Start a new one — write something in chat!",
    "notif.weekly_prize": "🎉 You are #{place} in this week's chatter top! Prize: 🪙 {prize} coins.",
    "common.no_pet": "🥚 No pet yet. Press /start and go through onboarding!",
    "common.back": "⬅️ Back",
}

CATALOGS: dict[str, dict[str, str]] = {
    "ru": _CATALOG_RU,
    "en": _CATALOG_EN,
}


def tf(lang: str | None, key: str, **params: Any) -> str:
    """Перевод по явному языку (для очередей уведомлений, где нет контекста)."""
    code = (lang or DEFAULT_LANG).lower()[:2]
    catalog = CATALOGS.get(code, _CATALOG_RU)
    text = catalog.get(key) or _CATALOG_RU.get(key) or key
    try:
        return text.format(**params) if params else text
    except (KeyError, IndexError):
        return text


def t(key: str, **params: Any) -> str:
    """Перевод в текущем языковом контексте (ставится UserLanguageMiddleware)."""
    return tf(get_current_lang(), key, **params)
