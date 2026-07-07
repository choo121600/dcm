"""Cross-study schedule injection ([study schedules]).

The curated STUDY_SCHEDULES table used to be wired only to /study-kickoff, so cross-study
schedule questions ("does anything run Sunday 7/19 14:00~17:00?") named no specific study,
matched nothing in study_lookup, and the persona deflected to "ask your mentor" even though
the bot had the data. Covers: the wants_schedule cue gate, the schedule_block renderer, and
end-to-end prompt injection through the orchestrator.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dcm.llm import LLMClient
from dcm.orchestrator import Orchestrator
from dcm.platform.base import IncomingMessage
from dcm.service.study_schedule import STUDY_SCHEDULES, schedule_block, wants_schedule


def run(coro):
    return asyncio.run(coro)


# ───────────────────────── wants_schedule (cue gate) ─────────────────────────


def test_wants_schedule_date_time_question():
    # The exact question shape that used to be deflected.
    assert wants_schedule("7월 19일 일요일 14시~17시에 예정된 강의가 있어?")


def test_wants_schedule_overlap_question():
    assert wants_schedule("스터디끼리 겹치는 거 있어?")
    assert wants_schedule("알고리즘 초급이랑 보안관제 동시에 들을 수 있어?")


def test_wants_schedule_day_and_when_cues():
    assert wants_schedule("레디스 스터디 언제 해?")
    assert wants_schedule("토요일에 뭐 있어?")
    assert wants_schedule("에어플로우 스케줄 알려줘")


def test_wants_schedule_time_patterns():
    assert wants_schedule("20:00에 하는 거 있나")
    assert wants_schedule("7/19에 뭐 해?")


def test_wants_schedule_ignores_chitchat():
    assert not wants_schedule("안녕!")
    assert not wants_schedule("밥 먹었어?")
    assert not wants_schedule("")
    assert not wants_schedule("오픈소스 스터디 멘토 누구야")


# ───────────────────────── schedule_block (renderer) ─────────────────────────


def test_schedule_block_lists_every_study():
    block = schedule_block()
    for study in STUDY_SCHEDULES.values():
        assert study.name in block
        assert study.mentor in block


def test_schedule_block_carries_confirmed_times():
    block = schedule_block()
    for study in STUDY_SCHEDULES.values():
        if study.confirmed:
            assert study.schedule in block


def test_schedule_block_marks_unconfirmed_with_note():
    block = schedule_block()
    unconfirmed = [s for s in STUDY_SCHEDULES.values() if not s.confirmed]
    assert unconfirmed  # curated data still has the TBD LLM study
    for study in unconfirmed:
        assert "미확정" in block  # ko default locale
        if study.note:
            assert study.note in block


# ───────────────────────── orchestrator injection (e2e) ─────────────────────────


def _incoming(text: str) -> IncomingMessage:
    return IncomingMessage(
        channel_id="ch1",
        author_id="u1",
        author_name="테스트유저",
        content=text,
        role_ids=frozenset(),
    )


def _build_llm() -> tuple[LLMClient, MagicMock]:
    cred = MagicMock()
    cred.label = "key1"
    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_block = MagicMock()
    fake_block.type = "text"
    fake_block.text = "응답 텍스트"
    fake_resp.content = [fake_block]
    fake_client.messages.create = AsyncMock(return_value=fake_resp)
    llm = LLMClient.__new__(LLMClient)
    llm._creds = [cred]
    llm._model = "claude-3-5-haiku-20241022"
    llm._max_tokens = 500
    llm._clients = {"key1": fake_client}
    return llm, fake_client


def _orchestrator(llm, tmp_path: Path) -> Orchestrator:
    persona = tmp_path / "persona.md"
    persona.write_text("테스트 페르소나", encoding="utf-8")
    return Orchestrator(
        llm=llm,
        persona_path=persona,
        bot_name="지우",
        max_input_chars=4000,
    )


def _last_user_content(fake_client) -> str:
    messages = fake_client.messages.create.call_args.kwargs["messages"]
    return messages[-1]["content"]


def test_schedule_table_injected_on_cross_study_time_question(tmp_path):
    llm, fake_client = _build_llm()
    orc = _orchestrator(llm, tmp_path)
    run(orc.handle(_incoming("7월 19일 일요일 14시~17시에 예정된 강의가 있어?"), []))
    content = _last_user_content(fake_client)
    assert "[study schedules" in content
    # The table itself (not just the label) is present, so the model can check overlaps.
    assert "알고리즘(중급)" in content
    assert "매주 일요일 20:00~22:00" in content


def test_schedule_table_absent_on_chitchat(tmp_path):
    llm, fake_client = _build_llm()
    orc = _orchestrator(llm, tmp_path)
    run(orc.handle(_incoming("안녕! 뭐하고 있었어"), []))
    content = _last_user_content(fake_client)
    assert "[study schedules" not in content
