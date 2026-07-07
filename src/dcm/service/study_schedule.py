"""Curated per-study schedule data + kickoff-message builder (discord-free).

Schedules are extracted from the SUSC study source
(https://raw.githubusercontent.com/SUSC-KR/susc/main/src/content/study/) as of 2026-07.
The source keeps schedules as free-text prose in the doc body (there is no structured field),
so this module is a hand-curated snapshot — keep it in sync when the source changes, or
normalize the source frontmatter and parse it instead.

Keyed by the mdx filename (i.e. `study_lookup.match_study`'s return value) so a Discord
category can be mapped to a study by running `match_study` on the category name.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..i18n import t


@dataclass(frozen=True)
class StudySchedule:
    name: str  # studyName (also the expected Discord category name)
    mentor: str
    schedule: str | None  # None = not yet fixed → coordinate with the mentor
    note: str | None = None  # extra hint (e.g. the mentor's preferred slots) for unconfirmed studies

    @property
    def confirmed(self) -> bool:
        return self.schedule is not None


# Schedule/overlap question cues — any hit injects the compact schedule table into the prompt.
# Broader than study_lookup's per-study keywords: cross-study questions ("does anything run
# Sunday 14:00?", "do any studies overlap?") name no specific study, so they need their own gate.
_SCHEDULE_CUES = (
    "일정", "스케줄", "시간표", "몇 시", "몇시", "언제", "요일",
    "겹치", "겹쳐", "겹침", "동시에", "같은 시간",
    "월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일",
    "schedule", "overlap",
)
# Explicit date/time mentions: "14시", "14:00", "7/19", "7월 19일", "7월".
_TIME_RE = re.compile(r"\d{1,2}\s*(?:시|:\d{2})|\d{1,2}\s*/\s*\d{1,2}|\d{1,2}\s*월(?:\s*\d{1,2}\s*일)?")


def wants_schedule(text: str) -> bool:
    """True when the message looks like a schedule/overlap question (day/time/date/overlap cue)."""
    low = (text or "").lower()
    return any(c in low for c in _SCHEDULE_CUES) or bool(_TIME_RE.search(low))


def schedule_block() -> str:
    """Compact all-studies schedule table (one line per study) for prompt injection.

    Static bundled data — no I/O. Injected by the orchestrator only when `wants_schedule`
    fires, so ordinary chit-chat pays no token cost.
    """
    lines = []
    for study in STUDY_SCHEDULES.values():
        if study.confirmed:
            sched = study.schedule
        else:
            sched = t("study.schedule_tbd")
            if study.note:
                sched += t("study.schedule_note", note=study.note)
        lines.append(t("study.schedule_line", name=study.name, mentor=study.mentor, schedule=sched))
    return "\n".join(lines)


# filename (match_study return) -> schedule. Order follows the study_lookup lineup.
# Times normalized to 24h only where the source is unambiguous; kept verbatim otherwise
# (e.g. RISC-V "7~9시" — the source does not state AM/PM, so it is not invented here).
STUDY_SCHEDULES: dict[str, StudySchedule] = {
    "summer_algorithm_basic.mdx": StudySchedule(
        "알고리즘(초급)", "김민상", "매주 토요일 20:00~22:00 (7/12~8/9)"
    ),
    "summer_algorithm_mid.mdx": StudySchedule(
        "알고리즘(중급)", "김민상", "매주 일요일 20:00~22:00 (7/13~8/10)"
    ),
    "summer_security.mdx": StudySchedule(
        "보안관제 AI에게 짬때리기", "임상빈", "매주 토요일 13:30~16:30"
    ),
    "summer_linux_monitering.mdx": StudySchedule(
        "리눅스 모니터링/옵저버빌리티 입문", "정규석", "매주 수요일 20:00~21:00"
    ),
    "summer_risc-v.mdx": StudySchedule(
        "RISC-V 어셈블리와 RVV 최적화", "문성준", "매주 화·목요일 7~9시"
    ),
    "summer_airflow.mdx": StudySchedule(
        "Apache Airflow를 활용한 워크플로우 자동화", "추영욱", "매주 화요일 20:00~22:00 (7/7~8/11)"
    ),
    "summer_opensource.mdx": StudySchedule(
        "오픈소스에 기여해보기", "추영욱", "매주 목요일 20:00~22:00 (7/9~8/6)"
    ),
    "summer_vibe.mdx": StudySchedule(
        "Claude Code를 활용한 바이브 코딩", "추영욱", "매주 수요일 20:00~22:00 (7/8~8/5)"
    ),
    "summer_redis.mdx": StudySchedule(
        "북스터디 (개발자를 위한 레디스)", "황제연", "매주 토요일 22:00~23:00"
    ),
    "summer_llm.mdx": StudySchedule(
        "LLM, 실무에서는 진짜 어떻게 쓸까?",
        "김현우",
        None,
        note="멘토 선호: 3~7시 / 9~11시, 목요일 제외 희망",
    ),
}


def build_study_message(study: StudySchedule, *, mention: str = "@here") -> str:
    """Assemble the per-study kickoff message (discord-free, i18n-resolved).

    `mention` is placed verbatim at the top so it pings when sent as message content.
    """
    lines = [mention, "", t("study.kickoff_intro"), ""]
    if study.confirmed:
        lines.append(t("study.kickoff_confirmed", name=study.name, schedule=study.schedule))
    else:
        lines.append(t("study.kickoff_unconfirmed", name=study.name))
        if study.note:
            lines.append(t("study.kickoff_note", note=study.note))
    lines.append(t("study.kickoff_mentor", mentor=study.mentor))
    lines += ["", t("study.kickoff_screenshot")]
    return "\n".join(lines)
