from __future__ import annotations

import re

# Lightweight intent detection for self-service memory commands (ARCHITECTURE.md §13.5, §14.2).
# These act only on the asking user's OWN memories, so they are privacy-safe by construction.

_FORGET = re.compile(r"(잊어|날\s*잊|기억.*(지워|삭제|없애)|forget\s*me)", re.IGNORECASE)
_SHOW = re.compile(r"(내.*기억|뭐.*기억|기억.*보여|what do you remember)", re.IGNORECASE)


def detect(text: str) -> str | None:
    """Return 'forget_me' | 'show_memories' | None for a mention's text."""
    if _FORGET.search(text):
        return "forget_me"
    if _SHOW.search(text):
        return "show_memories"
    return None
