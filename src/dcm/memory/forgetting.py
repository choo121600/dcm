from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .scoring import half_life_seconds, recency, retention
from .store import MemoryStore

if TYPE_CHECKING:
    from ..embeddings import Embedder
    from ..llm import LLMClient

log = logging.getLogger(__name__)

_BLUR_SYSTEM = (
    "Rewrite this memory as a shorter, more abstract one-sentence gist, dropping specific "
    "details but keeping the essence. Reply with the sentence only, no quotes."
)


async def run_pruning(
    store: MemoryStore,
    *,
    now: float,
    threshold: float,
    max_delete_ratio: float,
    half_life_base_days: float,
    mode: str = "delete",
    llm: LLMClient | None = None,
    embedder: Embedder | None = None,
    blur_model: str | None = None,
) -> dict[str, int]:
    """Forget low-retention memories (ARCHITECTURE.md §5.5).

    retention = importance_norm * recency * (1 + ln(1 + access_count)). Below `threshold` a
    'normal' memory is deleted (or, in blur mode, abstracted once before a later deletion).
    pinned/core are exempt. A per-run deletion cap (`max_delete_ratio`) is a safety rail.
    """
    normals = store.normal_memories()
    if not normals:
        return {"scanned": 0, "deleted": 0, "blurred": 0}

    doomed: list[tuple[float, object]] = []
    for mem in normals:
        half_life = half_life_seconds(mem.importance, half_life_base_days)
        rec = recency(now, mem.last_access_at, half_life)
        keep = retention(mem.importance, rec, mem.access_count)
        if keep < threshold:
            doomed.append((keep, mem))

    doomed.sort(key=lambda x: x[0])  # lowest retention first
    cap = max(1, int(len(normals) * max_delete_ratio))
    selected = [m for _, m in doomed[:cap]]

    deleted = blurred = 0
    for mem in selected:
        if mode == "blur" and not mem.blurred and llm and embedder:
            try:
                gist, _ = await llm.complete(
                    system=_BLUR_SYSTEM,
                    messages=[{"role": "user", "content": mem.content}],
                    model=blur_model,
                    max_tokens=120,
                )
                gist = gist.strip()
                emb = (await embedder.embed([gist]))[0]
                store.blur(mem.id, gist, emb, now)
                blurred += 1
                continue
            except Exception:
                log.exception("blur failed for #%s; falling back to delete", mem.id)
        store.delete([mem.id], reason=f"retention<{threshold}", now=now)
        deleted += 1

    log.info(
        "pruning: scanned=%d deleted=%d blurred=%d (of %d below threshold)",
        len(normals),
        deleted,
        blurred,
        len(doomed),
    )
    return {"scanned": len(normals), "deleted": deleted, "blurred": blurred}
