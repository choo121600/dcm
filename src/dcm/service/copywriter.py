"""Polishing promo/announcement copy (uses an LLM, discord-free).

Polishes **only the tone** of the announcement/event copy the staff enter. It never changes or
invents facts like dates/times/places/links, and on failure it returns the original text as-is
(fail-open policy). Since the date/countdown labels of an event announcement are decided by code
separately from this copy, polishing does not compromise factual accuracy.
"""
from __future__ import annotations

from ..i18n import t

MAX_POLISHED_CHARS = 600  # upper bound so a single announcement block isn't excessive


def _system(bot_name: str, kind: str) -> str:
    what = t("copywriter.kind_event") if kind == "event" else t("copywriter.kind_notice")
    return t("copywriter.system", bot_name=bot_name, what=what)


async def polish_copy(
    llm,
    *,
    bot_name: str,
    raw: str,
    kind: str = "event",
    title: str | None = None,
) -> str:
    """Polish and return the staff copy `raw`. On empty input/LLM failure/empty response, return the original as-is."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    header = t("copywriter.user_header", title=title.strip()) if title and title.strip() else ""
    user = f"{header}{t('copywriter.user_body', raw=raw)}"
    try:
        text, _ = await llm.complete(
            system=_system(bot_name, kind),
            messages=[{"role": "user", "content": user}],
        )
    except Exception:  # noqa: BLE001 - on LLM failure keep the original (don't block the announcement itself)
        return raw
    text = (text or "").strip().strip('"').strip()
    return text[:MAX_POLISHED_CHARS] if text else raw
