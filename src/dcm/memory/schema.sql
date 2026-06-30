-- Memory store (DESIGN.md §6).
-- M2 stores embeddings as a float32 BLOB and does brute-force cosine in Python — simple and
-- dependency-light at personal/small-guild scale. The sqlite-vec virtual table can replace this
-- later behind the same MemoryStore interface when scale demands it.

CREATE TABLE IF NOT EXISTS memories (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  kind           TEXT NOT NULL,              -- episodic | semantic | self
  subject_id     TEXT,                       -- related person (author id) or topic key
  guild_id       TEXT,                       -- owning guild (per-guild isolation; backfilled by _migrate)
  channel_id     TEXT,
  content        TEXT NOT NULL,              -- the memory, normalized to 1-2 sentences
  importance     REAL NOT NULL,              -- 1-10, scored by the LLM at ingest time
  created_at     REAL NOT NULL,
  last_access_at REAL NOT NULL,              -- updated on retrieval (reinforcement)
  access_count   INTEGER NOT NULL DEFAULT 0,
  protection     TEXT NOT NULL DEFAULT 'normal',  -- normal | pinned | core
  blurred        INTEGER NOT NULL DEFAULT 0,      -- 1 = already abstracted once (gradual forgetting)
  source_ids     TEXT,                       -- JSON array (reflection sources)
  embedding      BLOB NOT NULL               -- float32 array
);

CREATE INDEX IF NOT EXISTS idx_subject ON memories(subject_id);
CREATE INDEX IF NOT EXISTS idx_kind ON memories(kind);

-- Deletion log / archive (DESIGN.md §5.5, §12) — forgetting is irreversible, keep an audit trail.
CREATE TABLE IF NOT EXISTS forgotten_memories (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  original_id  INTEGER,
  kind         TEXT,
  subject_id   TEXT,
  guild_id     TEXT,
  content      TEXT,
  importance   REAL,
  reason       TEXT,
  forgotten_at REAL NOT NULL
);
