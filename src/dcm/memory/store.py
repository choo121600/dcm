from __future__ import annotations

import array
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..embeddings import cosine
from .scoring import half_life_seconds, recency, retrieval_score

log = logging.getLogger(__name__)

_SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


@dataclass
class Memory:
    id: int
    kind: str
    subject_id: str | None
    channel_id: str | None
    content: str
    importance: float
    created_at: float
    last_access_at: float
    access_count: int
    protection: str
    blurred: int
    source_ids: list[int]
    embedding: list[float]


def _to_blob(vec: list[float]) -> bytes:
    return array.array("f", vec).tobytes()


def _from_blob(blob: bytes) -> list[float]:
    a = array.array("f")
    a.frombytes(blob)
    return list(a)


class MemoryStore:
    """SQLite-backed memory store (DESIGN.md §5, §6).

    M2 does brute-force cosine in Python over candidate rows — fine at personal scale. sqlite3
    calls are synchronous; acceptable for low volume, swap to an executor / sqlite-vec later (M5).
    """

    def __init__(
        self,
        db_path: str,
        *,
        weights: tuple[float, float, float],
        half_life_base_days: float,
        subject_boost: float,
    ) -> None:
        path = Path(db_path)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._migrate()
        self._weights = weights
        self._half_life_base_days = half_life_base_days
        self._subject_boost = subject_boost

    def _migrate(self) -> None:
        # Add columns introduced after a DB may have been created (idempotent).
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(memories)")}
        if "blurred" not in cols:
            self._db.execute("ALTER TABLE memories ADD COLUMN blurred INTEGER NOT NULL DEFAULT 0")
            self._db.commit()

    def close(self) -> None:
        self._db.close()

    # --- writes ---

    def add(
        self,
        *,
        kind: str,
        content: str,
        importance: float,
        embedding: list[float],
        now: float,
        subject_id: str | None = None,
        channel_id: str | None = None,
        protection: str = "normal",
        source_ids: list[int] | None = None,
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO memories (kind, subject_id, channel_id, content, importance, "
            "created_at, last_access_at, access_count, protection, blurred, source_ids, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                subject_id,
                channel_id,
                content,
                float(importance),
                now,
                now,
                0,
                protection,
                0,
                json.dumps(source_ids or []),
                _to_blob(embedding),
            ),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def reinforce(self, ids: list[int], now: float) -> None:
        if not ids:
            return
        self._db.executemany(
            "UPDATE memories SET last_access_at = ?, access_count = access_count + 1 WHERE id = ?",
            [(now, i) for i in ids],
        )
        self._db.commit()

    def touch_importance(self, memory_id: int, importance: float, now: float) -> None:
        self._db.execute(
            "UPDATE memories SET importance = ?, last_access_at = ?, "
            "access_count = access_count + 1 WHERE id = ?",
            (float(importance), now, memory_id),
        )
        self._db.commit()

    def lower_importance(self, ids: list[int], factor: float, now: float) -> None:
        """Reduce importance of consolidated source memories so they fade (DESIGN.md §5.6)."""
        if not ids:
            return
        self._db.executemany(
            "UPDATE memories SET importance = importance * ? WHERE id = ?",
            [(factor, i) for i in ids],
        )
        self._db.commit()

    def blur(self, memory_id: int, content: str, embedding: list[float], now: float) -> None:
        """Replace content with a more abstract summary, mark blurred (gradual forgetting, §5.5)."""
        self._db.execute(
            "UPDATE memories SET content = ?, embedding = ?, blurred = 1, "
            "importance = MAX(1.0, importance * 0.6), last_access_at = ? WHERE id = ?",
            (content, _to_blob(embedding), now, memory_id),
        )
        self._db.commit()

    def delete(self, ids: list[int], *, reason: str, now: float) -> int:
        """Archive to forgotten_memories, then delete (DESIGN.md §5.5)."""
        if not ids:
            return 0
        rows = self._db.execute(
            f"SELECT * FROM memories WHERE id IN ({','.join('?' * len(ids))})", ids
        ).fetchall()
        self._db.executemany(
            "INSERT INTO forgotten_memories (original_id, kind, subject_id, content, "
            "importance, reason, forgotten_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (r["id"], r["kind"], r["subject_id"], r["content"], r["importance"], reason, now)
                for r in rows
            ],
        )
        self._db.execute(
            f"DELETE FROM memories WHERE id IN ({','.join('?' * len(ids))})", ids
        )
        self._db.commit()
        return len(rows)

    def forget_subject(self, subject_id: str, now: float) -> int:
        """Delete everything remembered about one person — the 'forget me' command (§14.2)."""
        rows = self._db.execute(
            "SELECT id FROM memories WHERE subject_id = ?", (subject_id,)
        ).fetchall()
        return self.delete([r["id"] for r in rows], reason="user requested forget", now=now)

    # --- reads ---

    def _candidates(self, *, subject_id: str | None, scope_to_subject: bool) -> list[Memory]:
        # Exclude 'self' memories from recall — they go into the system prompt separately (§4).
        # §14.3 exfiltration guard: when scoping, exclude memories about *other* specific people.
        if scope_to_subject:
            rows = self._db.execute(
                "SELECT * FROM memories WHERE kind != 'self' "
                "AND (subject_id IS NULL OR subject_id = ?)",
                (subject_id,),
            ).fetchall()
        else:
            rows = self._db.execute("SELECT * FROM memories WHERE kind != 'self'").fetchall()
        return [self._row_to_memory(r) for r in rows]

    def retrieve(
        self,
        query_embedding: list[float],
        *,
        now: float,
        subject_id: str | None = None,
        top_n: int = 6,
        scope_to_subject: bool = True,
    ) -> list[Memory]:
        w_rel, w_rec, w_imp = self._weights
        scored: list[tuple[float, Memory]] = []
        for mem in self._candidates(subject_id=subject_id, scope_to_subject=scope_to_subject):
            rel = cosine(query_embedding, mem.embedding)
            half_life = half_life_seconds(mem.importance, self._half_life_base_days)
            rec = recency(now, mem.last_access_at, half_life)
            score = retrieval_score(rel, rec, mem.importance, w_rel, w_rec, w_imp)
            if subject_id and mem.subject_id == subject_id:
                score += self._subject_boost  # recall the asker's own context first (§5.4)
            scored.append((score, mem))
        scored.sort(key=lambda s: s[0], reverse=True)
        top = [mem for _, mem in scored[:top_n]]
        self.reinforce([mem.id for mem in top], now)  # reinforcement (§5.4)
        return top

    def most_similar(
        self, embedding: list[float], *, subject_id: str | None = None
    ) -> tuple[float, Memory] | None:
        """Nearest existing memory by cosine — used for dedup at ingest (§5.3)."""
        best: tuple[float, Memory] | None = None
        for mem in self._candidates(subject_id=subject_id, scope_to_subject=False):
            rel = cosine(embedding, mem.embedding)
            if best is None or rel > best[0]:
                best = (rel, mem)
        return best

    def normal_memories(self) -> list[Memory]:
        """Forgettable memories (protection='normal') — the pruning candidate set (§5.5)."""
        rows = self._db.execute(
            "SELECT * FROM memories WHERE protection = 'normal'"
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def episodic_memories(self) -> list[Memory]:
        rows = self._db.execute(
            "SELECT * FROM memories WHERE kind = 'episodic'"
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def self_memories(self, limit: int = 5) -> list[Memory]:
        rows = self._db.execute(
            "SELECT * FROM memories WHERE kind = 'self' "
            "ORDER BY importance DESC, last_access_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def list_for_subject(self, subject_id: str, limit: int = 10) -> list[Memory]:
        rows = self._db.execute(
            "SELECT * FROM memories WHERE subject_id = ? "
            "ORDER BY importance DESC, last_access_at DESC LIMIT ?",
            (subject_id, limit),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"])

    def stats(self) -> dict[str, int]:
        rows = self._db.execute(
            "SELECT kind, COUNT(*) AS c FROM memories GROUP BY kind"
        ).fetchall()
        out = {r["kind"]: r["c"] for r in rows}
        out["total"] = self.count()
        out["forgotten"] = int(
            self._db.execute("SELECT COUNT(*) AS c FROM forgotten_memories").fetchone()["c"]
        )
        return out

    def _row_to_memory(self, row: sqlite3.Row) -> Memory:
        return Memory(
            id=row["id"],
            kind=row["kind"],
            subject_id=row["subject_id"],
            channel_id=row["channel_id"],
            content=row["content"],
            importance=row["importance"],
            created_at=row["created_at"],
            last_access_at=row["last_access_at"],
            access_count=row["access_count"],
            protection=row["protection"],
            blurred=row["blurred"],
            source_ids=json.loads(row["source_ids"] or "[]"),
            embedding=_from_blob(row["embedding"]),
        )
