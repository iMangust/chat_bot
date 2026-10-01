"""Стек навигации по разделам бота («Назад» туда, откуда пришёл).

Проблема, которую это решает: раньше почти на каждом экране нижняя кнопка
вела жёстко в главное меню («🏠 Меню» → menu:main), а «Назад» либо был
прибит к корню раздела, либо вёл не туда (например, из карточки товара
«Назад» мог привести к выбору питомца). Теперь при входе в раздел/экран
источник перехода кладётся в стек (по chat_id), а кнопки строятся так:

  • «⬅️ Назад» — возвращает ровно туда, откуда пользователь пришёл
    (callback источника из вершины стека);
  • «🏠 Меню» — сбрасывает стек и ведёт в главное меню.

Хранение: Redis (переживает рестарт бота) с in-memory-фолбэком — как в
остальных кулдаунах проекта (см. app/utils/redis.py). Если Redis недоступен
и памяти нет (рестарт процесса), стек пуст — кнопки деградируют до
безопасных корней разделов, ничего не ломая.
"""
from __future__ import annotations

import json
from collections import deque

from loguru import logger

from app.utils.redis import redis_client

MAX_DEPTH = 6          # глубина стека на чат
TTL_SEC = 24 * 3600    # стек живёт сутки без активности

# Локальный фолбэк, если Redis недоступен (сбрасывается при рестарте).
_mem: dict[int, deque[str]] = {}


def _key(chat_id: int) -> str:
    return f"nav:{chat_id}"


def _trim(cb: str) -> str:
    """Telegram ограничивает callback_data 64 байтами."""
    return cb[:64]


def mem_stack(chat_id: int | None) -> list[str]:
    """Синхронный доступ к локальному фолбэку стека (для сборки клавиатур в
    синхронных функциях). Redis-вершина оттуда же обновляется при каждом
    remember/pop_until/forget, поэтому в рамках одного процесса данные
    актуальны; при недоступном Redis это основной источник истории."""
    if chat_id is None:
        return []
    return list(_mem.get(int(chat_id), ()))


async def _load(chat_id: int) -> deque[str]:
    r = redis_client
    if r is not None:
        try:
            raw = await r.get(_key(chat_id))
            if raw:
                items = deque(json.loads(raw))
                if items:
                    return items
        except Exception as exc:  # noqa: BLE001
            logger.debug("nav stack load failed: {}", type(exc).__name__)
    return _mem.get(int(chat_id), deque())


async def _save(chat_id: int, stack: deque[str]) -> None:
    _mem[int(chat_id)] = stack
    r = redis_client
    if r is None:
        return
    try:
        await r.set(_key(chat_id), json.dumps(list(stack)), ex=TTL_SEC)
    except Exception as exc:  # noqa: BLE001
        logger.debug("nav stack save failed: {}", type(exc).__name__)


# Коллбэки-«шум»: клики внутри экрана (стрелки пагинации, действия), а не
# переходы между экранами. В стек не кладём, чтобы «Назад» не возвращал
# на ту же страницу, где пользователь уже сидит.
# Переключение вкладок/периодов внутри одного экрана («День/Неделя/Всё
# время», категории топов) — тоже клик внутри страницы, не переход.
_IGNORED_PREFIXES = ("onb:", "pet:page:", "shop:page:", "inv:page:",
                     "style:page:", "ach:page:", "top:page:", "top:")
_IGNORED_EXACT = {"menu:main", "menu:home", "pet:noop", "shop:noop",
                  "inv:noop", "style:noop", "ach:noop", "top:noop",
                  "arena:noop", "game:noop", "guess:noop", "rps:noop",
                  "bj:noop", "ev:noop", "merch:noop", "madmin:noop",
                  "fr:noop", "set:noop", "card:noop", "noop"}


def _is_noise(cb: str) -> bool:
    if cb in _IGNORED_EXACT:
        return True
    if cb.startswith(_IGNORED_PREFIXES):
        return True
    # стрелки пагинации вида 'xxx:back' / 'xxx:next'. Исключение — входы
    # в разделы ('menu:merch', 'menu:events', ...): это настоящие переходы,
    # они и есть точки возврата.
    parts = cb.split(":")
    if len(parts) >= 2 and parts[-1] in ("back", "next") and parts[0] != "menu":
        return True
    return False


async def remember(chat_id: int | None, from_cb: str | None) -> None:
    """Вызывать при входе в экран/раздел: from_cb — callback, по которому
    пришли сюда (значение нажатой кнопки). Пусто / главные кнопки игнорируются."""
    if chat_id is None or not from_cb:
        return
    if _is_noise(from_cb):
        return
    stack = await _load(chat_id)
    # дедупликация: повторный вход в тот же экран не плодит копии записи
    while from_cb in stack:
        stack.remove(from_cb)
    stack.append(_trim(from_cb))
    while len(stack) > MAX_DEPTH:
        stack.popleft()
    await _save(chat_id, stack)


async def back_target(chat_id: int | None, default: str) -> str:
    """Вершина стека — экран, куда вести кнопку «⬅️ Назад»; иначе default."""
    if chat_id is None:
        return default
    stack = await _load(chat_id)
    return stack[-1] if stack else default


async def pop_until(chat_id: int | None, prefixes: tuple[str, ...],
                    default: str) -> str:
    """Возврат «через несколько уровней»: снять со стека всё, что относится
    к текущему разделу (префиксы), пока снизу не окажется экран другого
    раздела — туда и ведём «Назад». Пример: Мерч → Категория → Товар,
    «Назад» с товара снимает 'merch:cat' и 'menu:merch' и возвращает в то
    меню, откуда когда-то зашли в мерч.

    ВАЖНО: порядок префиксов — от более специфичного к общему ('merch:'
    раньше 'menu:merch'), потому что startswith('merch:') матчит и
    'merch:...', и префиксные формы."""
    if chat_id is None:
        return default
    stack = await _load(chat_id)
    changed = False
    while stack and any(stack[-1].startswith(p) for p in prefixes):
        stack.pop()
        changed = True
    if changed:
        await _save(chat_id, stack)
    return stack[-1] if stack else default


async def forget(chat_id: int | None) -> None:
    """Сброс стека — вызывается при выходе в главное меню."""
    if chat_id is None:
        return
    _mem.pop(int(chat_id), None)
    r = redis_client
    if r is None:
        return
    try:
        await r.delete(_key(chat_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug("nav stack clear failed: {}", type(exc).__name__)
