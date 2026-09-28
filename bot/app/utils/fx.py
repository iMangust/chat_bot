"""Визуальные эффекты реакций на действия с питомцем.

Telegram не даёт «частиц» как в Tamagotchi-приложениях, но есть два
нативных канала визуальной обратной связи:

1. ⚡ Toast — всплывающая подпись под кнопкой (``cb.answer(text)``):
   появляется мгновенно прямо у пальца пользователя, без нового сообщения.
2. 😀 Реакции на сообщение (``message.react(...)``): карточка питомца
   «оживает» эмодзи-эффектом — ❤️ при кормёжке, 🫧 при мытье, 💤 при сне,
   🎉 при победе и т.д. Работает через Bot API 7.2+ (aiogram >= 3.13).

Оба канала best-effort: при ошибке API (старый сервер, нет прав на реакции
в группе и пр.) молча проходим дальше — эффект украшение, а не функциональность.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SetMessageReaction
from aiogram.types import CallbackQuery, Message, ReactionTypeEmoji

# Сколько случайных «второстепенных» эмодзи подмешивать к основному эффекту
_JITTER_POOL = ["✨", "🌟", "💫", "⭐"]


@dataclass(frozen=True)
class Effect:
    primary: tuple[str, ...]           # обязательные реакции на сообщении
    toast: str                         # текст всплывающей подписи под кнопкой
    extra_pool: tuple[str, ...] = ()   # из этого выбирается 0..1 случайной реакции
    extra_chance: float = 0.5          # вероятность подмешать случайную реакцию


# Эффекты по действиям тамагочи. Ключи совпадают с именами действий сервиса.
EFFECTS: dict[str, Effect] = {
    "feed":      Effect(("😋", "❤️"), "😋 Ням-ням!"),
    "play":      Effect(("🥳",),      "🎮 Игра состоялась!"),
    "win":       Effect(("🎉", "🤩"), "🏆 Победа!", ("✨",), 0.8),
    "lose":      Effect(("😿",),      "😿 В этот раз не вышло…"),
    "sleep":     Effect(("💤", "🌙"), "💤 Тссс… питомец уснул"),
    "wake":      Effect(("☀️", "🥱"), "☀️ Доброе утро!"),
    "wash":      Effect(("🫧", "🛁"), "🫧 Чистота!"),
    # Реакция на карточку = эмодзи КАЧАЕМОГО показателя (v1.5.66):
    # 💪 Сила · 🏃 Ловкость · 🧠 Интеллект — раньше у ловкости стояло ⚡,
    # что путало с Энергией (⚡ Энергия) и выглядело «не той» реакцией.
    "train_str": Effect(("💪",), "💪 Силовая тренировка!"),
    "train_agi": Effect(("🏃",), "🏃 Ловкость растёт!"),
    "train_int": Effect(("🧠",), "🧠 Интеллект качается!"),
    "walk":      Effect(("🌳", "🐾"), "🐾 На прогулку!"),
    "heal":      Effect(("💊", "❤️‍🩹"), "💊 Лечение прошло успешно"),
    "item":      Effect(("🎁", "✨"), "🎁 Предмет применён!"),
    "equip":     Effect(("🛡️", "✨"), "🛡️ Экипировано!"),
    "unequip":   Effect(("🎒",),      "🎒 Снято в инвентарь"),
    "coin":      Effect(("🪙", "💰"), "🪙 Монетки капают!"),
    "levelup":   Effect(("🎉", "🌟", "👑"), "⭐ Новый уровень!", ("✨",), 1.0),
}

_TRAIN_TOAST = {"strength": "train_str", "agility": "train_agi", "intellect": "train_int"}


def effect_for(action: str, stat: str | None = None) -> Effect | None:
    """Эффект по имени действия; для тренировок уточняется тип стата."""
    if action == "train" and stat:
        action = _TRAIN_TOAST.get(stat, "train_str")
    return EFFECTS.get(action)


async def react_to_message(cb: CallbackQuery, emoji: str, *, bot=None) -> None:
    """Безопасная эмодзи-реакция бота на сообщение-карточку (best-effort).

    Единственно правильный путь для aiogram 3.x: НЕ использовать shortcut
    ``message.react(str)`` — конструктор метода валидирует поле ``reaction``
    списком объектов ReactionType, и «сырая» строка-эмодзи выбивает pydantic
    ValidationError ещё ДО try/except (см. логи 13:32). Здесь: типизированный
    ReactionTypeEmoji; бот берётся из ``cb.bot`` (его подставляет сам
    Диспетчер aiogram при вызове хендлеров). Если бот недоступен или сервер
    отклонил реакцию — молча выходим: тост под кнопкой пользователь уже видел.
    """
    msg = cb.message
    # getattr, а не прямое обращение: тестовые/кастомные заглушки сообщения
    # могут не иметь chat/bot — в этом случае эффект просто пропускаем.
    chat = getattr(msg, "chat", None)
    if msg is None or chat is None or not emoji:
        return
    if bot is None:
        bot = getattr(cb, "bot", None) or getattr(msg, "bot", None)
    if bot is None:
        return
    try:
        await bot(
            SetMessageReaction(
                chat_id=msg.chat.id,
                message_id=msg.message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )
        )
    except Exception:
        pass   # эффект — украшение; тост под кнопкой уже показал результат


async def apply_effect(cb: CallbackQuery, action: str, *,
                       stat: str | None = None,
                       toast_override: str | None = None,
                       bot=None) -> None:
    """Один вызов = тост под кнопкой + реакции на сообщение-карточку.

    ``toast_override`` позволяет заменить дефолтный текст тоста (например,
    текстом результата действия, если он короткий).
    """
    eff = effect_for(action, stat)
    toast = toast_override or (eff.toast if eff else "")
    if toast:
        try:
            await cb.answer(toast[:200])   # лимит answerCallbackQuery — 200 симв.
        except TelegramAPIError:
            pass
    if not eff or cb.message is None:
        return
    emoji = list(eff.primary)
    if eff.extra_pool and random.random() < eff.extra_chance:
        emoji.append(random.choice(eff.extra_pool))
    msg: Message | None = cb.message
    if msg is None:
        return
    # Telegram разрешает боту одну реакцию на сообщение — ставим главную.
    await react_to_message(cb, emoji[0])
