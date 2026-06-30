"""Offline tests for M3 forgetting + the forget-me deletion (no keys/network).

Run: PYTHONPATH=src python tests/test_forgetting.py
"""
import asyncio
import tempfile
from pathlib import Path

from dcm.embeddings import LocalEmbedder
from dcm.memory.forgetting import run_pruning
from dcm.memory.store import MemoryStore

DAY = 86_400.0


async def main() -> None:
    emb = LocalEmbedder()
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(
            str(Path(tmp) / "m.db"),
            weights=(0.55, 0.20, 0.25),
            half_life_base_days=3.0,
            subject_boost=0.1,
            seed_guild_id="1",
        )
        now = 1_000_000.0
        old = now - 60 * DAY

        async def add(content, importance, *, when, protection="normal", subject="u1"):
            vec = (await emb.embed([content]))[0]
            return store.add(kind="episodic", content=content, importance=importance,
                             embedding=vec, now=when, subject_id=subject, protection=protection)

        trivial = await add("u1 mentioned the weather once", 2, when=old)
        important = await add("u1's mother is seriously ill", 9, when=old)
        pinned = await add("u1 pinned: birthday is March 3", 2, when=old, protection="pinned")

        res = await run_pruning(store, now=now, threshold=0.05, max_delete_ratio=0.5,
                                half_life_base_days=3.0, mode="delete")
        remaining = {m.id for m in store.list_for_subject("u1", limit=50)}
        assert trivial not in remaining, "trivial old memory should be forgotten"
        assert important in remaining, "important memory should survive"
        assert pinned in remaining, "pinned memory is exempt"
        assert res["deleted"] == 1, res
        assert store.stats().get("forgotten", 0) == 1, store.stats()

        # forget-me: delete only the asking user's memories.
        await add("u2 likes hiking", 6, when=now, subject="u2")
        deleted = store.forget_subject("u1", now)
        assert deleted >= 1
        assert not store.list_for_subject("u1", limit=50)
        assert store.list_for_subject("u2", limit=50)

        store.close()
    print("✅ test_forgetting passed")


if __name__ == "__main__":
    asyncio.run(main())

def test_forgetting_core() -> None:
    """pytest entry — runs the offline forgetting suite (ralplan S1: collectable regression gate)."""
    asyncio.run(main())
