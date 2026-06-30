"""Offline tests for the memory core — LocalEmbedder + MemoryStore (no keys/network).

Run: PYTHONPATH=src python tests/test_memory.py
"""
import asyncio
import tempfile
from pathlib import Path

from dcm.embeddings import LocalEmbedder
from dcm.memory.store import MemoryStore


async def main() -> None:
    emb = LocalEmbedder()
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(
            str(Path(tmp) / "m.db"),
            weights=(0.55, 0.20, 0.25),
            half_life_base_days=3.0,
            subject_boost=0.1,
        )
        now = 1_000_000.0

        facts = {
            "userA": [
                ("userA likes spicy food a lot", 5),
                ("userA changed jobs to a new startup", 8),
                ("userA mentioned the weather today", 2),
            ],
            "userB": [("userB is allergic to peanuts", 9)],
        }
        for subj, items in facts.items():
            vecs = await emb.embed([c for c, _ in items])
            for (content, imp), vec in zip(items, vecs):
                store.add(kind="episodic", content=content, importance=imp,
                          embedding=vec, now=now, subject_id=subj, channel_id="c1")
        assert store.count() == 4

        # Relevance ranking.
        q = (await emb.embed(["does userA like spicy food"]))[0]
        top = store.retrieve(q, now=now, subject_id="userA", top_n=3)
        assert top and "spicy" in top[0].content, top

        # §14.3 scope guard: userB's private memory must not surface for userA.
        q2 = (await emb.embed(["peanut allergy"]))[0]
        leaked = [m for m in store.retrieve(q2, now=now, subject_id="userA", top_n=10)
                  if m.subject_id == "userB"]
        assert not leaked, leaked

        # Reinforcement bumps access_count.
        again = store.retrieve(q, now=now, subject_id="userA", top_n=1)
        assert again[0].access_count >= 1

        # Dedup mechanism: token-identical content matches near-perfectly.
        dup = (await emb.embed(["spicy food a lot userA likes"]))[0]
        sim = store.most_similar(dup, subject_id="userA")
        assert sim and sim[0] > 0.99, sim

        store.close()
    print("✅ test_memory passed")


if __name__ == "__main__":
    asyncio.run(main())

def test_memory_core() -> None:
    """pytest entry — runs the offline memory-core suite (ralplan S1: collectable regression gate)."""
    asyncio.run(main())
