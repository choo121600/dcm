"""P5 서버(길드)별 기억 격리 테스트 — for_guild 핸들이 WHERE guild_id 를 강제하는지."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from dcm.memory.store import MemoryStore


def _store(path: str) -> MemoryStore:
    return MemoryStore(
        path, weights=(0.55, 0.2, 0.25), half_life_base_days=3.0, subject_boost=0.1, seed_guild_id="0"
    )


def _add(handle, content, *, subject="u1", kind="episodic", emb=None):
    return handle.add(
        kind=kind,
        content=content,
        importance=5.0,
        embedding=emb or [0.1, 0.2, 0.3],
        now=1000.0,
        subject_id=subject,
    )


def test_retrieve_is_isolated_by_guild():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(str(Path(tmp) / "m.db"))
        _add(s.for_guild("A"), "A의 비밀", emb=[1.0, 0.0, 0.0])
        _add(s.for_guild("B"), "B의 비밀", emb=[1.0, 0.0, 0.0])
        ra = s.for_guild("A").retrieve([1.0, 0.0, 0.0], now=1001.0, subject_id="u1", top_n=10)
        rb = s.for_guild("B").retrieve([1.0, 0.0, 0.0], now=1001.0, subject_id="u1", top_n=10)
        assert [m.content for m in ra] == ["A의 비밀"]
        assert [m.content for m in rb] == ["B의 비밀"]
        s.close()


def test_most_similar_is_isolated_by_guild():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(str(Path(tmp) / "m.db"))
        _add(s.for_guild("A"), "A 사실", emb=[1.0, 0.0, 0.0])
        assert s.for_guild("B").most_similar([1.0, 0.0, 0.0], subject_id="u1") is None
        sim_a = s.for_guild("A").most_similar([1.0, 0.0, 0.0], subject_id="u1")
        assert sim_a is not None and sim_a[1].content == "A 사실"
        s.close()


def test_forget_subject_is_per_guild():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(str(Path(tmp) / "m.db"))
        _add(s.for_guild("A"), "A-u1", subject="u1")
        _add(s.for_guild("B"), "B-u1", subject="u1")
        assert s.for_guild("A").forget_subject("u1", 1002.0) == 1
        assert s.for_guild("A").count() == 0
        assert s.for_guild("B").count() == 1  # 다른 길드의 같은 user 기억은 보존
        s.close()


def test_self_memories_per_guild():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(str(Path(tmp) / "m.db"))
        s.for_guild("A").add(kind="self", content="A에서의 나", importance=8.0, embedding=[0.1], now=1000.0)
        assert [m.content for m in s.for_guild("A").self_memories()] == ["A에서의 나"]
        assert s.for_guild("B").self_memories() == []
        s.close()


def test_episodic_memories_isolated_for_reflection():
    # reflection 은 핸들의 episodic_memories() 로 그룹핑 → source_ids 가 단일 길드로 한정됨
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(str(Path(tmp) / "m.db"))
        _add(s.for_guild("A"), "a-ep")
        _add(s.for_guild("B"), "b-ep")
        assert [m.content for m in s.for_guild("A").episodic_memories()] == ["a-ep"]
        assert [m.content for m in s.for_guild("B").episodic_memories()] == ["b-ep"]
        s.close()


def test_guild_ids_and_scoped_counts():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(str(Path(tmp) / "m.db"))
        _add(s.for_guild("A"), "a1")
        _add(s.for_guild("A"), "a2")
        _add(s.for_guild("B"), "b1")
        assert set(s.guild_ids()) == {"A", "B"}
        assert s.for_guild("A").count() == 2
        assert s.for_guild("B").count() == 1
        assert s.count() == 3  # 전역(스코프 없음)


def test_handle_add_writes_guild_id_no_null_orphan():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "m.db")
        s = _store(path)
        s.for_guild("77").add(
            kind="episodic", content="x", importance=5.0, embedding=[0.1], now=1000.0, subject_id="u"
        )
        db = sqlite3.connect(path)
        try:
            nulls = db.execute("SELECT COUNT(*) FROM memories WHERE guild_id IS NULL").fetchone()[0]
            g = db.execute("SELECT guild_id FROM memories WHERE content='x'").fetchone()[0]
        finally:
            db.close()
        s.close()
        assert nulls == 0  # 핸들 경유 add 는 NULL orphan 미생성
        assert g == "77"
