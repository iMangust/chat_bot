from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

from app.db.repositories import UserRepository
from app.utils.local_time import now as local_now
from app.utils.safe_edit import safe_edit_or_answer

router = Router(name="events")

EVENTS_FILE = Path("data/events.json")


def _load_events() -> list[dict[str, Any]]:
    try:
        raw = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError):
        logger.warning("events: файл {} повреждён — игнорирую", EVENTS_FILE)
        return []
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict)]


def _upcoming(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    today = local_now().date().isoformat()
    out = [e for e in events if str(e.get("date", "")) >= today]
    out.sort(key=lambda e: str(e.get("date", "")))
    return out


async def _render_events_screen(cb: CallbackQuery, session) -> None:
    users = UserRepository(session)
    await users.get_or_create(cb.from_user.id, cb.from_user.first_name or "",
                              cb.from_user.username)
    await session.commit()
    events = _upcoming(_load_events())
    if not events:
        text = ("<b>📅 Мероприятия канала</b>\n\n"
                "Скоро тут появятся анонсы стримов, встреч и розыгрышей 🎉\n"
                "Загляни позже или следи за новостями в канале!")
    else:
        lines = ["<b>📅 Предстоящие мероприятия</b>", ""]
        for e in events[:10]:
            icon = e.get("icon", "🎪")
            date = e.get("date", "?")
            time = f" · {e['time']}" if e.get("time") else ""
            place = f"\n   📍 {e['place']}" if e.get("place") else ""
            desc = f"\n   {e['description']}" if e.get("description") else ""
            lines.append(f"{icon} <b>{e.get('title', 'Без названия')}</b> — {date}{time}{place}{desc}")
        if len(events) > 10:
            lines.append(f"\n…и ещё {len(events) - 10} 🔜")
        text = "\n".join(lines)
    b = InlineKeyboardBuilder()
    if events:
        url_btn = next((e.get("url") for e in events if e.get("url")), None)
        if url_btn:
            b.button(text="🔗 Подробнее о ближайшем", url=url_btn)
            b.row()
    b.button(text="🏠 Меню", callback_data="menu:main")
    await safe_edit_or_answer(cb.message, text, reply_markup=b.as_markup())


@router.callback_query(F.data == "menu:events")
async def menu_events(cb: CallbackQuery, session) -> None:
    await _render_events_screen(cb, session)
    await cb.answer()
