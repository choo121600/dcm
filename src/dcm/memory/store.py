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
    """SQLite-backed memory store (ARCHITECTURE.md §5, §6).

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
        seed_guild_id: str,
    ) -> None:
        path = Path(db_path)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._seed_guild_id = str(seed_guild_id)
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
        # Per-guild isolation: add guild_id and backfill legacy rows to the seed guild.
        # guild composite indexes are created HERE (after the column exists), never in schema.sql:
        # schema.sql runs before this migration and would crash a legacy DB ("no such column:
        # guild_id") if it indexed a not-yet-added column.
        if "guild_id" not in cols:
            self._db.execute("ALTER TABLE memories ADD COLUMN guild_id TEXT")
        fcols = {r["name"] for r in self._db.execute("PRAGMA table_info(forgotten_memories)")}
        if "guild_id" not in fcols:
            self._db.execute("ALTER TABLE forgotten_memories ADD COLUMN guild_id TEXT")
        # Backfill rows with no guild yet (legacy data) to the injected seed guild (idempotent:
        # later runs match 0 rows). Seed is injected by the caller, never hardcoded here.
        self._db.execute(
            "UPDATE memories SET guild_id = ? WHERE guild_id IS NULL", (self._seed_guild_id,)
        )
        self._db.execute(
            "UPDATE forgotten_memories SET guild_id = ? WHERE guild_id IS NULL",
            (self._seed_guild_id,),
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_guild_subject ON memories(guild_id, subject_id)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_guild_kind ON memories(guild_id, kind)")
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
        guild_id: str | None = None,
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO memories (kind, subject_id, guild_id, channel_id, content, importance, "
            "created_at, last_access_at, access_count, protection, blurred, source_ids, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                subject_id,
                guild_id,
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
        """Reduce importance of consolidated source memories so they fade (ARCHITECTURE.md §5.6)."""
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
        """Archive to forgotten_memories, then delete (ARCHITECTURE.md §5.5)."""
        if not ids:
            return 0
        rows = self._db.execute(
            f"SELECT * FROM memories WHERE id IN ({','.join('?' * len(ids))})", ids
        ).fetchall()
        self._db.executemany(
            "INSERT INTO forgotten_memories (original_id, kind, subject_id, guild_id, content, "
            "importance, reason, forgotten_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    r["id"], r["kind"], r["subject_id"], r["guild_id"], r["content"],
                    r["importance"], reason, now,
                )
                for r in rows
            ],
        )
        self._db.execute(
            f"DELETE FROM memories WHERE id IN ({','.join('?' * len(ids))})", ids
        )
        self._db.commit()
        return len(rows)

    def forget_subject(self, subject_id: str, now: float, *, guild_id: str | None = None) -> int:
        """Delete everything remembered about one person — the 'forget me' command (§14.2).
        Multi-guild (P5): when guild_id is given, delete only within that guild."""
        if guild_id is None:
            rows = self._db.execute(
                "SELECT id FROM memories WHERE subject_id = ?", (subject_id,)
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT id FROM memories WHERE subject_id = ? AND guild_id = ?",
                (subject_id, str(guild_id)),
            ).fetchall()
        return self.delete([r["id"] for r in rows], reason="user requested forget", now=now)

    # --- reads ---

    def _candidates(
        self, *, subject_id: str | None, scope_to_subject: bool, guild_id: str | None = None
    ) -> list[Memory]:
        # Exclude 'self' memories from recall — they go into the system prompt separately (§4).
        # §14.3 exfiltration guard: when scoping, exclude memories about *other* specific people.
        # Multi-guild (P5): when guild_id is given, restrict to that guild's memories only.
        where = ["kind != 'self'"]
        params: list = []
        if guild_id is not None:
            where.append("guild_id = ?")
            params.append(str(guild_id))
        if scope_to_subject:
            where.append("(subject_id IS NULL OR subject_id = ?)")
            params.append(subject_id)
        rows = self._db.execute(
            f"SELECT * FROM memories WHERE {' AND '.join(where)}", params
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def retrieve(
        self,
        query_embedding: list[float],
        *,
        now: float,
        subject_id: str | None = None,
        top_n: int = 6,
        scope_to_subject: bool = True,
        guild_id: str | None = None,
    ) -> list[Memory]:
        w_rel, w_rec, w_imp = self._weights
        scored: list[tuple[float, Memory]] = []
        for mem in self._candidates(
            subject_id=subject_id, scope_to_subject=scope_to_subject, guild_id=guild_id
        ):
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
        self, embedding: list[float], *, subject_id: str | None = None, guild_id: str | None = None
    ) -> tuple[float, Memory] | None:
        """Nearest existing memory by cosine — used for dedup at ingest (§5.3)."""
        best: tuple[float, Memory] | None = None
        for mem in self._candidates(subject_id=subject_id, scope_to_subject=False, guild_id=guild_id):
            rel = cosine(embedding, mem.embedding)
            if best is None or rel > best[0]:
                best = (rel, mem)
        return best

    def normal_memories(self, *, guild_id: str | None = None) -> list[Memory]:
        """Forgettable memories (protection='normal') — the pruning candidate set (§5.5)."""
        if guild_id is None:
            rows = self._db.execute("SELECT * FROM memories WHERE protection = 'normal'").fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM memories WHERE protection = 'normal' AND guild_id = ?",
                (str(guild_id),),
            ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def episodic_memories(self, *, guild_id: str | None = None) -> list[Memory]:
        if guild_id is None:
            rows = self._db.execute("SELECT * FROM memories WHERE kind = 'episodic'").fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM memories WHERE kind = 'episodic' AND guild_id = ?", (str(guild_id),)
            ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def self_memories(self, limit: int = 5, *, guild_id: str | None = None) -> list[Memory]:
        if guild_id is None:
            rows = self._db.execute(
                "SELECT * FROM memories WHERE kind = 'self' "
                "ORDER BY importance DESC, last_access_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM memories WHERE kind = 'self' AND guild_id = ? "
                "ORDER BY importance DESC, last_access_at DESC LIMIT ?",
                (str(guild_id), limit),
            ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def list_for_subject(
        self, subject_id: str, limit: int = 10, *, guild_id: str | None = None
    ) -> list[Memory]:
        if guild_id is None:
            rows = self._db.execute(
                "SELECT * FROM memories WHERE subject_id = ? "
                "ORDER BY importance DESC, last_access_at DESC LIMIT ?",
                (subject_id, limit),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM memories WHERE subject_id = ? AND guild_id = ? "
                "ORDER BY importance DESC, last_access_at DESC LIMIT ?",
                (subject_id, str(guild_id), limit),
            ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def count(self, *, guild_id: str | None = None) -> int:
        if guild_id is None:
            return int(self._db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"])
        return int(
            self._db.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE guild_id = ?", (str(guild_id),)
            ).fetchone()["c"]
        )

    def stats(self, *, guild_id: str | None = None) -> dict[str, int]:
        if guild_id is None:
            rows = self._db.execute(
                "SELECT kind, COUNT(*) AS c FROM memories GROUP BY kind"
            ).fetchall()
            forgotten = self._db.execute(
                "SELECT COUNT(*) AS c FROM forgotten_memories"
            ).fetchone()["c"]
        else:
            rows = self._db.execute(
                "SELECT kind, COUNT(*) AS c FROM memories WHERE guild_id = ? GROUP BY kind",
                (str(guild_id),),
            ).fetchall()
            forgotten = self._db.execute(
                "SELECT COUNT(*) AS c FROM forgotten_memories WHERE guild_id = ?", (str(guild_id),)
            ).fetchone()["c"]
        out = {r["kind"]: r["c"] for r in rows}
        out["total"] = self.count(guild_id=guild_id)
        out["forgotten"] = int(forgotten)
        return out

    def guild_ids(self) -> list[str]:
        """All guild ids that have memories (for per-guild iteration by background jobs, P5)."""
        rows = self._db.execute(
            "SELECT DISTINCT guild_id FROM memories WHERE guild_id IS NOT NULL"
        ).fetchall()
        return [r["guild_id"] for r in rows]

    def for_guild(self, guild_id: int | str) -> "_GuildScopedStore":
        """guild_id-bound handle — structurally injects WHERE guild_id into every read/write (P5)."""
        return _GuildScopedStore(self, str(guild_id))

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


class _GuildScopedStore:
    """A MemoryStore handle bound to a guild_id (P5). Structurally injects WHERE guild_id into
    every read/write so a caller cannot cause cross-guild leakage by forgetting guild_id.
    id-based operations (reinforce/delete/lower_importance/blur) only receive ids already obtained
    from guild-scoped reads, so they are delegated as-is. reflection/forgetting take this handle
    as their store and work unchanged (same interface)."""

    def __init__(self, store: MemoryStore, guild_id: str) -> None:
        self._s = store
        self._gid = str(guild_id)

    @property
    def guild_id(self) -> str:
        return self._gid

    def add(self, **kw) -> int:
        return self._s.add(guild_id=self._gid, **kw)

    def retrieve(self, query_embedding, **kw):
        return self._s.retrieve(query_embedding, guild_id=self._gid, **kw)

    def most_similar(self, embedding, **kw):
        return self._s.most_similar(embedding, guild_id=self._gid, **kw)

    def reinforce(self, ids, now) -> None:
        self._s.reinforce(ids, now)

    def lower_importance(self, ids, factor, now) -> None:
        self._s.lower_importance(ids, factor, now)

    def blur(self, memory_id, content, embedding, now) -> None:
        self._s.blur(memory_id, content, embedding, now)

    def touch_importance(self, memory_id, importance, now) -> None:
        self._s.touch_importance(memory_id, importance, now)

    def delete(self, ids, *, reason, now) -> int:
        return self._s.delete(ids, reason=reason, now=now)

    def normal_memories(self):
        return self._s.normal_memories(guild_id=self._gid)

    def episodic_memories(self):
        return self._s.episodic_memories(guild_id=self._gid)

    def self_memories(self, limit: int = 5):
        return self._s.self_memories(limit=limit, guild_id=self._gid)

    def list_for_subject(self, subject_id, limit: int = 10):
        return self._s.list_for_subject(subject_id, limit=limit, guild_id=self._gid)

    def forget_subject(self, subject_id, now) -> int:
        return self._s.forget_subject(subject_id, now, guild_id=self._gid)

    def count(self) -> int:
        return self._s.count(guild_id=self._gid)

    def stats(self) -> dict:
        return self._s.stats(guild_id=self._gid)
