"""현재 스터디 상세 문서 온디맨드 읽기 (discord-free).

전체 커리큘럼을 시스템 프롬프트에 상주시키면 매 메시지마다 토큰이 크게 든다. 대신
프롬프트에는 스터디 목록+링크만 두고, 사용자가 특정 스터디를 '깊이' 물을 때만 그 문서
(GitHub 원문 mdx)를 읽어와 그 메시지에만 컨텍스트로 주입한다.
- 얕은 질문(무슨 스터디 있어 / 멘토 누구 / 언제)은 인덱스로 답 → 문서 안 읽음.
- 깊은 질문(커리큘럼 / 뭐 배워 / 계획)일 때만 해당 문서 1건을 읽음(전체 문서 매번 X).
결과는 캐시하고, 실패하면 조용히 None(요약+링크로 폴백). 참가자 명단(PII)은 제외.
"""
from __future__ import annotations

import asyncio
import urllib.request

_RAW_BASE = "https://raw.githubusercontent.com/SUSC-KR/susc/main/src/content/study/"

# 스터디 키워드(소문자) → mdx 파일명 (현재 26 Summer 라인업).
_STUDY_FILES: list[tuple[tuple[str, ...], str]] = [
    (("알고리즘 초급", "알고리즘(초급)", "알고리즘초급", "알고초급", "algorithm_basic"), "summer_algorithm_basic.mdx"),
    (("알고리즘 중급", "알고리즘(중급)", "알고리즘중급", "알고중급", "algorithm_mid"), "summer_algorithm_mid.mdx"),
    (("보안관제", "보안 관제", "opensearch", "siem", "보안스터디", "보안 스터디"), "summer_security.mdx"),
    (("리눅스", "모니터링", "옵저버빌리티", "observability"), "summer_linux_monitering.mdx"),
    (("risc-v", "riscv", "risc", "리스크파이브", "어셈블리", "rvv"), "summer_risc-v.mdx"),
    (("airflow", "에어플로우", "에어플로", "워크플로우 자동화"), "summer_airflow.mdx"),
    (("오픈소스", "opensource", "기여"), "summer_opensource.mdx"),
    (("claude code", "클로드 코드", "바이브 코딩", "바이브코딩", "vibe"), "summer_vibe.mdx"),
    (("redis", "레디스"), "summer_redis.mdx"),
    (("llm 실무", "llm 스터디", "엘엘엠", "llm,"), "summer_llm.mdx"),
]

# '깊이(커리큘럼/계획/내용)' 신호 — 이게 있어야 문서를 읽는다. 멘토/시간은 인덱스로 답하므로 제외.
_DETAIL_CUES = (
    "커리큘럼", "커리", "계획", "일정", "주차", "몇 주", "배우", "배워", "배울", "내용",
    "자세", "설명", "목표", "어떻게", "진행", "알려", "궁금", "뭐 하", "뭐해", "뭐 배", "무엇",
)


def match_study(text: str) -> str | None:
    """텍스트에 특정 스터디 키워드가 있으면 그 mdx 파일명, 없으면 None."""
    low = (text or "").lower()
    for keywords, filename in _STUDY_FILES:
        if any(k in low for k in keywords):
            return filename
    return None


def wants_detail(text: str) -> bool:
    low = (text or "").lower()
    return any(cue in low for cue in _DETAIL_CUES)


def _strip_frontmatter(mdx: str) -> str:
    """프런트매터(--- ... ---; memberNameList 등 PII 포함)를 떼고 본문만 반환."""
    s = mdx.lstrip()
    if s.startswith("---"):
        end = s.find("\n---", 3)
        if end != -1:
            s = s[end + 4:]
    return s.strip()


class StudyLookup:
    """스터디 상세 문서를 GitHub 원문에서 온디맨드로 읽어오는 조회기(캐시 + fail-open)."""

    def __init__(self, *, timeout: float = 6.0, max_chars: int = 2500) -> None:
        self._timeout = timeout
        self._max_chars = max_chars
        self._cache: dict[str, str] = {}

    def _blocking_fetch(self, filename: str) -> str:
        req = urllib.request.Request(_RAW_BASE + filename, headers={"User-Agent": "dcm-study-lookup"})
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return resp.read().decode("utf-8")

    async def maybe_fetch(self, text: str) -> str | None:
        """메시지가 특정 스터디를 '깊이' 물을 때만 그 문서 본문을 반환. 아니면 None(문서 안 읽음)."""
        filename = match_study(text)
        if not filename or not wants_detail(text):
            return None
        if filename in self._cache:
            return self._cache[filename]
        try:
            raw = await asyncio.to_thread(self._blocking_fetch, filename)
        except Exception:  # noqa: BLE001 - 조회 실패는 조용히 폴백(요약+링크로)
            return None
        body = _strip_frontmatter(raw)[: self._max_chars]
        if body:
            self._cache[filename] = body
        return body or None
