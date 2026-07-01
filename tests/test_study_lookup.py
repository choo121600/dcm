"""tests/test_study_lookup.py — 스터디 상세 온디맨드 읽기 (discord-free, 네트워크 없이)."""
from __future__ import annotations

import asyncio

from dcm.service.study_lookup import StudyLookup, match_study, wants_detail

_SAMPLE_MDX = (
    "---\n"
    'studyName: "Apache Airflow"\n'
    'mentorNames: ["추영욱"]\n'
    "memberNameList: ['김철수', '박영희']\n"  # PII — 본문에서 빠져야 함
    "---\n\n"
    "## 강의 목표\n워크플로우 자동화\n\n## 강의 계획\n- 1주차: 개념\n- 2주차: 실습\n"
)


def test_match_study_by_keyword():
    assert match_study("airflow 커리큘럼 알려줘") == "summer_airflow.mdx"
    assert match_study("레디스 스터디 뭐 배워?") == "summer_redis.mdx"
    assert match_study("보안관제 자세히") == "summer_security.mdx"
    assert match_study("알고리즘 중급 계획") == "summer_algorithm_mid.mdx"


def test_match_study_none_for_unrelated():
    assert match_study("오늘 날씨 좋다") is None
    assert match_study("안녕 반가워") is None


def test_wants_detail_cues():
    assert wants_detail("커리큘럼 알려줘")
    assert wants_detail("몇 주차에 뭐 배워?")
    assert not wants_detail("멘토 누구야")  # 얕은 질문(인덱스로 답) — 문서 안 읽음
    assert not wants_detail("언제 해?")


def _run(coro):
    return asyncio.run(coro)


def test_maybe_fetch_shallow_does_not_fetch():
    lookup = StudyLookup()
    called = {"n": 0}

    def _boom(_):
        called["n"] += 1
        raise AssertionError("얕은 질문인데 문서를 읽었음")

    lookup._blocking_fetch = _boom  # type: ignore[assignment]
    # 스터디 키워드는 있지만 depth 큐가 없음 → fetch 안 함
    assert _run(lookup.maybe_fetch("airflow 멘토 누구야")) is None
    assert called["n"] == 0


def test_maybe_fetch_deep_fetches_and_strips_pii():
    lookup = StudyLookup()
    lookup._blocking_fetch = lambda _f: _SAMPLE_MDX  # type: ignore[assignment]
    out = _run(lookup.maybe_fetch("airflow 커리큘럼 자세히 알려줘"))
    assert out is not None
    assert "강의 계획" in out and "1주차" in out
    assert "memberNameList" not in out and "김철수" not in out  # 프런트매터(PII) 제거
    assert "studyName" not in out


def test_maybe_fetch_caches():
    lookup = StudyLookup()
    calls = {"n": 0}

    def _once(_f):
        calls["n"] += 1
        return _SAMPLE_MDX

    lookup._blocking_fetch = _once  # type: ignore[assignment]
    _run(lookup.maybe_fetch("airflow 커리큘럼"))
    _run(lookup.maybe_fetch("airflow 계획 어떻게 돼"))
    assert calls["n"] == 1  # 같은 문서는 한 번만 fetch(캐시)


def test_maybe_fetch_failopen():
    lookup = StudyLookup()

    def _fail(_f):
        raise RuntimeError("network down")

    lookup._blocking_fetch = _fail  # type: ignore[assignment]
    assert _run(lookup.maybe_fetch("redis 커리큘럼 자세히")) is None  # 실패 시 조용히 None
