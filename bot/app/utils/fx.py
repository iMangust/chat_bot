from __future__ import annotations

import random
from dataclasses import dataclass

from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SetMessageReaction
from aiogram.types import CallbackQuery, Message, ReactionTypeEmoji

_JITTER_POOL = ["✨", "🌟", "💫", "⭐"]

@dataclass(frozen=True)
class Effect:
    primary: tuple[str, ...]
    toast: str
    extra_pool: tuple[str, ...] = ()
    extra_chance: float = 0.5

EFFECTS: dict[str, Effect] = {
    "feed":      Effect(("😋", "❤️"), "😋 Ням-ням!"),
    "play":      Effect(("🥳",),      "🎮 Игра состоялась!"),
    "win":       Effect(("🎉", "🤩"), "🏆 Победа!", ("✨",), 0.8),
    "lose":      Effect(("😿",),      "😿 В этот раз не вышло…"),
    "sleep":     Effect(("💤", "🌙"), "💤 Тссс… питомец уснул"),
    "wake":      Effect(("☀️", "🥱"), "☀️ Доброе утро!"),
    "wash":      Effect(("🫧", "🛁"), "🫧 Чистота!"),
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
    if action == "train" and stat:
        action = _TRAIN_TOAST.get(stat, "train_str")
    return EFFECTS.get(action)

async def react_to_message(cb: CallbackQuery, emoji: str, *, bot=None) -> None:
    msg = cb.message
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
        pass

async def apply_effect(cb: CallbackQuery, action: str, *,
                       stat: str | None = None,
                       toast_override: str | None = None,
                       bot=None) -> None:
    from app import themes

    eff = themes.themed_effect(effect_for(action, stat)) \
        if effect_for(action, stat) else None
    toast = toast_override or (eff.toast if eff else "")
    if toast:
        try:
            await cb.answer(toast[:200])
        except TelegramAPIError:
            pass
    if not eff or cb.message is None:
        return
    emoji = list(eff.primary)
    if eff.extra_pool and random.random() < eff.extra_chance:
        emoji.append(random.choice(eff.extra_pool))
    # Реакцию ставим ТОЛЬКО на исходное сообщение с нажатой кнопкой и
    # только если оно не будет отредактировано результатом (иначе
    # Telegram показывает реакцию как «реакцию на ответ бота» рядом со
    # строкой результата — визуально это выглядит багом).
    text = getattr(cb.message, "text", None) or ""
    if text.startswith(("Ты:", "🎲", "🃏")):
        return
    await react_to_message(cb, emoji[0])
