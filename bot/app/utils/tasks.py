"""Fire-and-forget asyncio tasks with a strong reference.

asyncio only keeps a weak reference to bare tasks: without storing the return
value of create_task()/ensure_future(), a task can be garbage-collected mid
flight (see RUF006). This helper keeps the required strong reference and
cleans it up when the task finishes, logging any unhandled exception instead
of letting it vanish silently.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine

_pending: set[asyncio.Task] = set()

logger = logging.getLogger(__name__)


def spawn(coro: Coroutine, *, name: str | None = None) -> asyncio.Task:
    """Schedule *coro* as a task, keeping a strong reference until it is done."""
    task = asyncio.create_task(coro, name=name) if name else asyncio.create_task(coro)
    _pending.add(task)

    def _done(t: asyncio.Task) -> None:
        _pending.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.warning("fire-and-forget task %s failed: %r", t.get_name(), exc)

    task.add_done_callback(_done)
    return task
