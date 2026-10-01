from __future__ import annotations

from typing import Any

DEFAULT_LANG = "ru"

STRINGS: dict[str, str] = {

    "pet.eaten": "🍎 Ням-ням{tail}",
    "pet.sleeping_deny": "😴 Питомец спит — не мешай!",
    "pet.too_tired_play": "😩 Питомец слишком устал для игр. Пусть поспит!",
    "pet.won_game": "🎉 Победа! Питомец в восторге! +{xp} XP",
    "pet.lost_game": "🙂 Не повезло, но питомцу всё равно весело. +{xp} XP",
    "pet.already_sleeping": "😴 Он уже спит.",
    "pet.fell_asleep": "💤 Питомец уснул до {time} (камчатское время). Энергия будет восстанавливаться во сне.",
    "pet.not_sleeping": "🐱 Он и не спит.",
    "pet.woken": "⏰ Ты разбудил питомца! Проспал {hours} ч → накоплено ⚡ +{energy} энергии (уже учтено на карточке).",
    "pet.sleeping_deny_feed": "😴 Во сне не кормят! Сначала разбуди (кнопка «⏰ Разбудить»).",
    "pet.sleeping_deny_train": "😴 Спящий питомец не тренируется — разбуди его сначала.",
    "pet.sleeping_deny_heal": "😴 Лекарство во сне не дают — разбуди питомца и вылечи.",
    "pet.washed": "🛁 Чистенький и пахучий! Гигиена +40",
    "pet.healed": "💊 Лечение помогло! Здоровье +35",
    "pet.not_sick": "😀 Питомец здоров, лекарство не нужно.",
    "pet.walk_started": "🚶 Питомец ушёл гулять на {hours} ч. Вернётся в {time} — с новостями!",
    "pet.walk_already": "🚶 Питомец уже гуляет… Вернётся в {time} (через {minutes}).",
    "pet.not_walking": "🏡 Он и не гуляет — дома.",
    "pet.walk_returned_early": "🚶 Ты позвал питомца домой! Погулял {hours} ч → {reward} (уже учтено на карточке).",
    "pet.walk_back_line": "🚶 Сейчас на прогулке… Вернётся в {time} (через {minutes}).",
    "pet.walk_deny_sleep": "🚶 {name} гуляет — спать на улице нельзя! Дождись возвращения.",
    "pet.walk_deny_wash": "🚿 {name} гуляет — мыться на прогулке негде! Дождись возвращения.",
    "pet.walk_deny_train": "🏋️ {name} гуляет — тренироваться на улице негде! Дождись возвращения.",
    "pet.walk_deny_medicine": "💊 {name} гуляет — лекарства на прогулке не дают! Дождись возвращения.",
    "pet.train_done": "🏋️ Тренировка завершена! {label} +{gain}",
    "pet.train_stat": "💪 Сила", "pet.train_agi": "🏃 Ловкость", "pet.train_int": "🧠 Интеллект",
    "pet.cooldown_feed": "⏳ Питомец только что ел! Подожди {sec} сек.",
    "pet.cooldown_generic": "⏳ Перерыв между действиями: {sec} сек.",
    "pet.critical_deny": "🚨 {name} в критическом состоянии! Обычный уход уже не поможет — "
                         "нужна «💖 Реанимация» на странице «Уход» (200 🪙).",
    "pet.critical_banner": "🚨 <b>Критическое состояние!</b> Здоровье и один из показателей на нуле. "
                           "Спасай: «💖 Реанимация» за 200 🪙 или усынови нового питомца.",
    "pet.no_more_lives": "🥀 У {name} больше не осталось жизней — реанимация недоступна. "
                         "Можно только усыновить нового питомца (старый уйдёт в историю).",
    "pet.revive_done": "💖 {name} реанимирован! За это снято ⭐ {stars}.",
    "pet.revive_no_money": "💰 Не хватает монет: нужно {need}, у тебя {have}. "
                           "Зарабатывай активностью в чате!",
    "pet.not_critical": "✅ Питомец в порядке — реанимация не нужна.",
    "pet.history_empty": "📜 История пуста: это твой первый питомец!",
    "pet.history_title": "📜 <b>История питомцев</b>",
    "pet.mood_great": "Великолепно!",
    "pet.mood_good": "Хорошее настроение",
    "pet.mood_ok": "Нормально",
    "pet.mood_sad": "Грустит… удели внимание",
    "pet.mood_sick": "Больной! Нужно лечение 💊",
    "pet.mood_sleeping": "Спит… не буди 💤",
    "pet.mood_hungry": "Голодный! Дай поесть 🍎",
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
    "shop.title": "🛒 <b>Магазин питомца</b> · у тебя 🪙 {coins}",
    "shop.bought": "Куплено: {icon} {name}!",
    "shop.no_pet": "Нет питомца или пользователя",
    "shop.not_enough": "Не хватает {missing} монет 🪙",
    "inv.empty": "🎒 Инвентарь пуст. Загляни в 🛒 Магазин!",
    "notif.pet_sad": "🐾 {name} {reason}",
    "notif.streak_burned": "🔥 Твоя серия дней сгорела. Начни новую — напиши что-нибудь в чат!",
    "notif.weekly_prize": "🎉 Ты #{place} в недельном топе болтунов! Приз: 🪙 {prize} монет.",
    "common.no_pet": "🥚 У тебя пока нет питомца. Нажми /start и пройди онбординг!",
    "common.back": "⬅️ Назад",
}

def tf(lang: str | None, key: str, **params: Any) -> str:
    from app import themes

    text = STRINGS.get(key) or key
    text = themes.tr(key, text)  # подмена строк/эмодзи активной темой
    try:
        return text.format(**params) if params else text
    except (KeyError, IndexError):
        return text

def t(key: str, **params: Any) -> str:
    return tf(DEFAULT_LANG, key, **params)
