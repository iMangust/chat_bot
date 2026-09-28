"""Нарезка длинных сообщений под лимит Telegram (4096 символов).

Любой «длинный экран» (/help, простыня топов, карточка с погодой) теперь
режется по границам строк с запасом в 4000 символов — раньше единое сообщение
стабильно падало с TelegramBadRequest («… is too long»), из-за чего справка
вообще не открывалась.
"""
from __future__ import annotations

import re

TG_LIMIT = 4096
SAFE_LIMIT = 4000

def split_message(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Режет текст по строкам; одна строка длиннее лимита — режется жёстко."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append("\n".join(cur))
                cur, cur_len = [], 0
            cut = line.rfind(" ", 0, limit)
            cut = limit if cut < limit // 2 else cut
            chunks.append(line[:cut])
            line = line[cut:].lstrip(" ")
        add = len(line) + (1 if cur else 0)
        if cur and cur_len + add > limit:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
            add = len(line)
        cur.append(line)
        cur_len += add
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in (ch.strip("\n") for ch in chunks) if c]

def strip_html_tags(text: str) -> str:
    """Убирает теги Telegram HTML (для plain-фолбэка)."""
    return re.sub(r"</?(b|i|u|s|code|pre|a|tg-spoiler)[^>]*>", "", text)
