from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from .store import MemoryStore

if TYPE_CHECKING:
    from ..embeddings import Embedder
    from ..llm import LLMClient

log = logging.getLogger(__name__)

_DISTILL_SYSTEM = (
    "You are the memory of a Discord bot, consolidating scattered notes about one person into "
    "higher-level understanding. Given several episodic notes, output ONLY a JSON array of "
    'distilled facts: {"content": "<1 sentence, third person>", "importance": <int 1-10>}. '
    "Produce at most 3, only genuinely supported generalizations. If nothing rises above the "
    "notes themselves, return []."
)

_SELF_SYSTEM = (
    "You are a Discord bot reflecting on who you are becoming, based on recent things you've "
    "learned and talked about. Output ONLY a JSON array (at most 2) of first-person self-notes: "
    '{"content": "<1 sentence, e.g. lately I am into X / my role here is Y>", '
    '"importance": <int 1-10>}. Return [] if nothing notable.'
)


def _parse(raw: str) -> list[dict]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            first, rest = text.split("\n", 1)
            if first.strip().lower() in {"json", ""}:
                text = rest
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("reflection: could not parse JSON: %.120s", text)
        return []
    return [d for d in data if isinstance(d, dict) and d.get("content")] if isinstance(data, list) else []


async def run_reflection(
    llm: LLMClient,
    store: MemoryStore,
    embedder: Embedder,
    *,
    now: float,
    min_episodics: int,
    model: str | None = None,
) -> dict[str, int]:
    """Consolidate episodics into semantic + self memories — growth (DESIGN.md §5.6)."""
    episodics = store.episodic_memories()
    groups: dict[str, list] = {}
    for mem in episodics:
        if mem.subject_id:
            groups.setdefault(mem.subject_id, []).append(mem)

    semantic_created = 0
    for subject, mems in groups.items():
        if len(mems) < min_episodics:
            continue
        notes = "\n".join(f"- {m.content}" for m in mems)
        try:
            raw, _ = await llm.complete(
                system=_DISTILL_SYSTEM,
                messages=[{"role": "user", "content": notes}],
                model=model,
                max_tokens=400,
            )
        except Exception:
            log.exception("reflection: distill failed for subject %s", subject)
            continue

        facts = _parse(raw)
        if not facts:
            continue
        embeddings = await embedder.embed([str(f["content"]).strip() for f in facts])
        source_ids = [m.id for m in mems]
        for fact, emb in zip(facts, embeddings):
            store.add(
                kind="semantic",
                content=str(fact["content"]).strip(),
                importance=float(max(1, min(10, int(fact.get("importance", 7))))),
                embedding=emb,
                now=now,
                subject_id=subject,
                source_ids=source_ids,
            )
            semantic_created += 1
        # Consolidated sources fade naturally now that their meaning is captured (§5.6).
        store.lower_importance(source_ids, factor=0.5, now=now)

    self_created = await _reflect_self(llm, store, embedder, now=now, model=model)

    log.info("reflection: semantic=%d self=%d", semantic_created, self_created)
    return {"semantic": semantic_created, "self": self_created}


async def _reflect_self(
    llm: LLMClient,
    store: MemoryStore,
    embedder: Embedder,
    *,
    now: float,
    model: str | None,
) -> int:
    recent = store.episodic_memories()[-30:]
    if not recent:
        return 0
    notes = "\n".join(f"- {m.content}" for m in recent)
    try:
        raw, _ = await llm.complete(
            system=_SELF_SYSTEM,
            messages=[{"role": "user", "content": notes}],
            model=model,
            max_tokens=300,
        )
    except Exception:
        log.exception("reflection: self-reflect failed")
        return 0

    facts = _parse(raw)
    if not facts:
        return 0
    embeddings = await embedder.embed([str(f["content"]).strip() for f in facts])
    for fact, emb in zip(facts, embeddings):
        store.add(
            kind="self",
            content=str(fact["content"]).strip(),
            importance=float(max(1, min(10, int(fact.get("importance", 7))))),
            embedding=emb,
            now=now,
            subject_id=None,
            protection="core",  # self-memory is near-permanent (§4, §5.1)
        )
    return len(facts)
