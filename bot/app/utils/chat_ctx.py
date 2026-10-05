from __future__ import annotations

"""LRU-контейнер «chat_id -> значение» с жёстким лимитом записей.

Вынесен из handlers/tamagotchi.py в utils, чтобы любые модули (игры,
магазин, соцраздел) могли использовать его без кросс-импортов хендлеров
и без циклических зависимостей.

Простые dict для чатного контекста растут без ограничений: каждый
когда-либо открывавший экран чат оставлял бы запись навсегда
(утечка памяти на долгих аптаймах). LRU на maxsize решает это без
Redis: порядок доступа поддерживается move_to_end, вытеснение —
oldest-first.
"""

from collections import OrderedDict


class BoundedChatCtx:
    """LRU-словарь чатов: get/set/pop + dict-подобные операции.

    Все записи проходят через set()/__setitem__, которые поддерживают
    LRU-порядок и вытесняют самые старые ключи при переполнении.
    """

    def __init__(self, maxsize: int = 4096) -> None:
        self._d: OrderedDict[int, object] = OrderedDict()
        self._max = maxsize

    def get(self, chat_id: int, default=None):
        key = int(chat_id)
        if key in self._d:
            self._d.move_to_end(key)   # freshest right
            return self._d[key]
        return default

    def set(self, chat_id: int, value) -> None:
        key = int(chat_id)
        self._d[key] = value
        self._d.move_to_end(key)
        while len(self._d) > self._max:
            self._d.popitem(last=False)

    def pop(self, chat_id: int, default=None):
        return self._d.pop(int(chat_id), default)

    # Совместимость с кодом, который читал словарь напрямую
    def __getitem__(self, chat_id: int):
        return self._d[chat_id]

    def __contains__(self, chat_id: int) -> bool:
        return int(chat_id) in self._d

    def __setitem__(self, chat_id: int, value) -> None:
        self.set(chat_id, value)

    def __len__(self) -> int:
        return len(self._d)
