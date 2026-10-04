from __future__ import annotations

from dataclasses import dataclass

from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

# ВАЖНО: бот НЕ ставит реакции на сообщения пользователя — это было багом
# (после мини-игры под итогом «Ты победил!» появлялась реакция 🎉/😿, а в
# магазине — 🪙). Единственный фидбэк на действия — тост (cb.answer) и
# текст сообщения-итога. Механика реакций в чатах принадлежит пользователям;
# их учёт (reactions_given/reactions_received) живёт в group tracking и
# никак не зависит от этого модуля.


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

async def apply_effect(cb: CallbackQuery, action: str, *,
                       stat: str | None = None,
                       toast_override: str | None = None,
                       bot=None,
                       message: Message | None = None,
                       react_target: Message | None = None) -> None:
    """Показать тост об игровом действии (кормёжка, игра, покупка…).

    Реакции на сообщения НЕ ставятся — только ephemeral-уведомление над
    кнопкой: пользователь не получает никаких изменений в чате. Параметры
    ``message``/``react_target`` приняты для совместимости со старыми
    вызовами и намеренно игнорируются.
    """
    from app import themes

    eff = themes.themed_effect(effect_for(action, stat)) \
        if effect_for(action, stat) else None
    toast = toast_override or (eff.toast if eff else "")
    if toast:
        try:
            await cb.answer(toast[:200])
        except TelegramAPIError:
            pass
        return
    try:
        await cb.answer()
    except TelegramAPIError:
        pass
