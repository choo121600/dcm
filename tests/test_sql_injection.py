"""DB(SQL) 인젝션 방어 회귀 테스트.

무신뢰 입력(메시지/이름/설정값)이 SQL 문자열로 연결되지 않고 항상 파라미터 바인딩으로만
저장되는지, 동적 컬럼명 경로(guild_settings._upsert)는 allowlist로 막히는지 검증한다.
즉 프롬프트로 SQL 페이로드가 들어와도 '데이터'로만 저장되고 실행되지 않는다.

추가 방어층: sqlite3 execute()는 한 번에 한 문장만 실행하므로, 설령 페이로드가 값으로
들어가도 두 번째 문장(`; DROP TABLE …`)은 파싱조차 되지 않는다(파라미터는 SQL로 해석 안 됨).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from dcm.memory.store import MemoryStore
from dcm.service.guild_settings import GuildSettingsStore

_PAYLOADS = [
    "'; DROP TABLE memories; --",
    "Robert'); DROP TABLE memories;--",
    '" OR "1"="1',
    "1); DELETE FROM memories WHERE (1=1",
]


def _mstore(path: str) -> MemoryStore:
    return MemoryStore(
        path, weights=(0.5, 0.25, 0.25), half_life_base_days=3.0, subject_boost=0.1, seed_guild_id="0"
    )


def test_memory_content_and_subject_sql_payload_stored_as_data():
    """SQL 페이로드를 content/subject로 저장해도 테이블이 살아있고 리터럴로 저장·조회된다."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _mstore(str(Path(tmp) / "m.db"))
        g = s.for_guild("G")
        for i, p in enumerate(_PAYLOADS):
            g.add(
                kind="episodic",
                content=p,
                importance=5.0,
                embedding=[0.1, 0.2, 0.3],
                now=1000.0 + i,
                subject_id=p,
            )
        # 테이블 생존 + 모든 행 보존(드롭/삭제 미실행)
        assert g.count() == len(_PAYLOADS)
        # subject_id 페이로드도 리터럴로 매칭되어 content가 그대로 조회됨
        rows = g.list_for_subject(_PAYLOADS[0])
        assert [m.content for m in rows] == [_PAYLOADS[0]]
        s.close()


def test_forget_subject_sql_payload_is_literal_only():
    """forget_subject에 SQL 페이로드 subject를 줘도 그 리터럴 행만 지우고 테이블은 생존한다."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _mstore(str(Path(tmp) / "m.db"))
        g = s.for_guild("G")
        evil = "'; DROP TABLE memories; --"
        g.add(kind="episodic", content="x", importance=5.0, embedding=[0.1, 0.2, 0.3], now=1000.0, subject_id=evil)
        g.add(kind="episodic", content="y", importance=5.0, embedding=[0.1, 0.2, 0.3], now=1001.0, subject_id="normal")
        removed = g.forget_subject(evil, 1002.0)
        assert removed == 1  # 페이로드 subject 1행만 삭제
        assert g.count() == 1  # 테이블 생존, normal 행 보존
        s.close()


def test_guild_settings_upsert_rejects_non_allowlist_column():
    """_upsert는 allowlist 밖 컬럼명을 거부 — 동적 컬럼명 인젝션 차단."""
    with tempfile.TemporaryDirectory() as tmp:
        st = GuildSettingsStore(str(Path(tmp) / "s.db"))
        with pytest.raises(ValueError):
            st._upsert("123", "welcome_message='' ; DROP TABLE guild_settings; --", "x")
        st.close()


def test_guild_settings_value_sql_payload_stored_as_data():
    """설정 VALUE에 SQL 페이로드를 넣어도 리터럴로 저장되고 테이블은 생존한다."""
    with tempfile.TemporaryDirectory() as tmp:
        st = GuildSettingsStore(str(Path(tmp) / "s.db"))
        payload = "'; DROP TABLE guild_settings; --"
        st.set_welcome_message("123", payload)
        assert st.get("123").welcome_message == payload  # 리터럴 저장·조회
        st.close()
