"""On-demand reading of the current study's detail docs (discord-free).

Keeping the entire curriculum resident in the system prompt costs a lot of tokens per message.
Instead, the prompt holds only the study list + links, and only when a user asks about a specific
study 'in depth' do we fetch that doc (the raw GitHub mdx) and inject it as context for that message
only.
- Shallow questions (what studies are there / who's the mentor / when) are answered from the index →
  the doc is not read.
- Only for deep questions (curriculum / what will I learn / plan) is that single doc read (not every
  doc every time).
The result is cached, and on failure it silently returns None (falling back to summary + link). The
participant roster (PII) is excluded.
"""
from __future__ import annotations

import asyncio
import urllib.request

_RAW_BASE = "https://raw.githubusercontent.com/SUSC-KR/susc/main/src/content/study/"

# Study keywords (lowercase) → mdx filename (current 26 Summer lineup).
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

# 'Depth (curriculum/plan/content)' signals — the doc is read only if one is present. Mentor/time are answered from the index, so excluded.
_DETAIL_CUES = (
    "커리큘럼", "커리", "계획", "일정", "주차", "몇 주", "배우", "배워", "배울", "내용",
    "자세", "설명", "목표", "어떻게", "진행", "알려", "궁금", "뭐 하", "뭐해", "뭐 배", "무엇",
)


def match_study(text: str) -> str | None:
    """If the text contains a specific study keyword, the corresponding mdx filename; otherwise None."""
    low = (text or "").lower()
    for keywords, filename in _STUDY_FILES:
        if any(k in low for k in keywords):
            return filename
    return None


def wants_detail(text: str) -> bool:
    low = (text or "").lower()
    return any(cue in low for cue in _DETAIL_CUES)


def _strip_frontmatter(mdx: str) -> str:
    """Strip the frontmatter (--- ... ---; contains PII like memberNameList) and return only the body."""
    s = mdx.lstrip()
    if s.startswith("---"):
        end = s.find("\n---", 3)
        if end != -1:
            s = s[end + 4:]
    return s.strip()


class StudyLookup:
    """Looks up study detail docs on demand from the raw GitHub source (cached + fail-open)."""

    def __init__(self, *, timeout: float = 6.0, max_chars: int = 2500) -> None:
        self._timeout = timeout
        self._max_chars = max_chars
        self._cache: dict[str, str] = {}

    def _blocking_fetch(self, filename: str) -> str:
        req = urllib.request.Request(_RAW_BASE + filename, headers={"User-Agent": "dcm-study-lookup"})
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return resp.read().decode("utf-8")

    async def maybe_fetch(self, text: str) -> str | None:
        """Returns the doc body only when the message asks about a specific study 'in depth'. Otherwise None (doc not read)."""
        filename = match_study(text)
        if not filename or not wants_detail(text):
            return None
        if filename in self._cache:
            return self._cache[filename]
        try:
            raw = await asyncio.to_thread(self._blocking_fetch, filename)
        except Exception:  # noqa: BLE001 - a lookup failure silently falls back (to summary + link)
            return None
        body = _strip_frontmatter(raw)[: self._max_chars]
        if body:
            self._cache[filename] = body
        return body or None
