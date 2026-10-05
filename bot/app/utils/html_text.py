from __future__ import annotations

from html import escape


def esc(value: object) -> str:
    return escape(str(value if value is not None else ""))
