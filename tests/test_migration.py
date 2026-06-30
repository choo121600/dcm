"""P1 guild_id 마이그레이션 테스트 — 레거시 DB 무크래시 부팅 + backfill + 멱등 + 인덱스."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from dcm.memory.store import MemoryStore

# guild_id 컬럼이 없던 시절의 스키마(운영 prod DB 형태: blurred 있음, guild_id 없음).
_LEGACY = """
CREATE TABLE memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  subject_id TEXT,
  channel_id TEXT,
  content TEXT NOT NULL,
  importance REAL NOT NULL,
  created_at REAL NOT NULL,
  last_access_at REAL NOT NULL,
  access_count INTEGER NOT NULL DEFAULT 0,
  protection TEXT NOT NULL DEFAULT 'normal',
  blurred INTEGER NOT NULL DEFAULT 0,
  source_ids TEXT,
  embedding BLOB NOT NULL
);
CREATE TABLE forgotten_memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  original_id INTEGER,
  kind TEXT,
  subject_id TEXT,
  content TEXT,
  importance REAL,
  reason TEXT,
  forgotten_at REAL NOT NULL
);
"""


def _make_legacy_db(path: str, rows: int = 3) -> None:
    db = sqlite3.connect(path)
    db.executescript(_LEGACY)
    for i in range(rows):
        db.execute(
            "INSERT INTO memories (kind, subject_id, content, importance, created_at, "
            "last_access_at, access_count, protection, blurred, source_ids, embedding) "
            "VALUES ('episodic', ?, ?, 5.0, 1000.0, 1000.0, 0, 'normal', 0, '[]', ?)",
            (f"u{i}", f"mem {i}", b"\x00\x00\x00\x00"),
        )
    db.execute(
        "INSERT INTO forgotten_memories (original_id, kind, subject_id, content, importance, "
        "reason, forgotten_at) VALUES (1, 'episodic', 'u0', 'gone', 3.0, 'prune', 1000.0)"
    )
    db.commit()
    db.close()


def _store(path: str, seed: str) -> MemoryStore:
    return MemoryStore(
        path, weights=(0.55, 0.2, 0.25), half_life_base_days=3.0, subject_boost=0.1, seed_guild_id=seed
    )


def _cols(path: str, table: str) -> set[str]:
    db = sqlite3.connect(path)
    try:
        return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    finally:
        db.close()


def _indexes(path: str) -> set[str]:
    db = sqlite3.connect(path)
    try:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        db.close()


def test_legacy_db_boots_without_crash_and_backfills():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "legacy.db")
        _make_legacy_db(path, rows=3)
        store = _store(path, seed="999888777")  # 임의 주입 seed (store에 하드코딩 아님)
        store.close()
        assert "guild_id" in _cols(path, "memories")
        assert "guild_id" in _cols(path, "forgotten_memories")
        db = sqlite3.connect(path)
        try:
            nulls = db.execute("SELECT COUNT(*) FROM memories WHERE guild_id IS NULL").fetchone()[0]
            seeded = db.execute(
                "SELECT COUNT(*) FROM memories WHERE guild_id = ?", ("999888777",)
            ).fetchone()[0]
            f_seeded = db.execute(
                "SELECT COUNT(*) FROM forgotten_memories WHERE guild_id = ?", ("999888777",)
            ).fetchone()[0]
        finally:
            db.close()
        assert nulls == 0
        assert seeded == 3
        assert f_seeded == 1
        assert {"idx_guild_subject", "idx_guild_kind"} <= _indexes(path)


def test_migration_idempotent_and_preserves_existing_guild():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "legacy.db")
        _make_legacy_db(path, rows=2)
        _store(path, seed="111").close()  # 1차: 전부 111로 backfill
        db = sqlite3.connect(path)
        db.execute("UPDATE memories SET guild_id = '222' WHERE id = 1")
        db.commit()
        db.close()
        _store(path, seed="333").close()  # 2차: NULL 아닌 행은 절대 덮어쓰지 않음
        db = sqlite3.connect(path)
        try:
            g1 = db.execute("SELECT guild_id FROM memories WHERE id = 1").fetchone()[0]
            g2 = db.execute("SELECT guild_id FROM memories WHERE id = 2").fetchone()[0]
        finally:
            db.close()
        assert g1 == "222"  # 기존 비-NULL 보존
        assert g2 == "111"  # 1차 backfill 값 유지(333으로 재시드 안 함)


def test_fresh_db_has_guild_id_and_indexes():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "fresh.db")
        _store(path, seed="42").close()
        assert "guild_id" in _cols(path, "memories")
        assert {"idx_guild_subject", "idx_guild_kind"} <= _indexes(path)


def test_add_persists_guild_id():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "fresh.db")
        store = _store(path, seed="42")
        mid = store.add(
            kind="episodic",
            content="hi",
            importance=5.0,
            embedding=[0.1, 0.2],
            now=1000.0,
            subject_id="u1",
            guild_id="777",
        )
        store.close()
        db = sqlite3.connect(path)
        try:
            g = db.execute("SELECT guild_id FROM memories WHERE id = ?", (mid,)).fetchone()[0]
        finally:
            db.close()
        assert g == "777"  # add()가 명시 guild_id를 그대로 저장
