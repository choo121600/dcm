from __future__ import annotations

import json
import logging

from ..embeddings import Embedder
from ..llm import LLMClient
from .store import MemoryStore

log = logging.getLogger(__name__)

_EXTRACT_SYSTEM = (
    "You extract long-term-worthy memories from a chat exchange for a Discord bot. "
    "Return ONLY a JSON array, no prose. Each item is "
    '{"content": "<1-2 sentences, third person, about the user>", "importance": <int 1-10>}. '
    "Importance guide: 1-3 trivial/small talk, 4-6 tastes or minor preferences, "
    "7-10 identity, relationships, or major events. "
    "If nothing is worth remembering long-term, return []."
)


def _parse_items(raw: str) -> list[dict]:
    text = raw.strip()
    if text.startswith("```"):  # tolerate fenced output
        text = text.strip("`")
        if "\n" in text:
            first, rest = text.split("\n", 1)
            if first.strip().lower() in {"json", ""}:
                text = rest
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("ingest: could not parse JSON from extraction: %.120s", text)
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("content")]


class IngestionPipeline:
    """Turns a finished exchange into stored memories (DESIGN.md §5.3). Runs off the response path."""

    def __init__(
        self,
        llm: LLMClient,
        store: MemoryStore,
        embedder: Embedder,
        *,
        ingest_model: str,
        dedup_threshold: float,
    ) -> None:
        self._llm = llm
        self._store = store
        self._embedder = embedder
        self._ingest_model = ingest_model
        self._dedup_threshold = dedup_threshold

    async def ingest(
        self,
        *,
        author_id: str,
        author_name: str,
        channel_id: str,
        user_text: str,
        bot_reply: str,
        now: float,
    ) -> None:
        exchange = f"{author_name}: {user_text}\n{bot_reply}"
        try:
            raw, _ = await self._llm.complete(
                system=_EXTRACT_SYSTEM,
                messages=[{"role": "user", "content": exchange}],
                model=self._ingest_model,
                max_tokens=400,
            )
        except Exception:
            log.exception("ingest: extraction call failed")
            return

        items = _parse_items(raw)
        if not items:
            return

        contents = [str(it["content"]).strip() for it in items]
        embeddings = await self._embedder.embed(contents)

        for item, content, embedding in zip(items, contents, embeddings):
            if not content:
                continue
            importance = float(max(1, min(10, int(item.get("importance", 3)))))

            similar = self._store.most_similar(embedding, subject_id=author_id)
            if similar and similar[0] >= self._dedup_threshold:
                # Reinforce the existing memory instead of duplicating (§5.3).
                self._store.touch_importance(
                    similar[1].id, max(similar[1].importance, importance), now
                )
                log.info("ingest: reinforced existing memory #%s", similar[1].id)
                continue

            memory_id = self._store.add(
                kind="episodic",
                content=content,
                importance=importance,
                embedding=embedding,
                now=now,
                subject_id=author_id,
                channel_id=channel_id,
            )
            log.info("ingest: stored memory #%s (importance=%.0f)", memory_id, importance)
