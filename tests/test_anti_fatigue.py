"""Anti-fatigue: 채널 독점 완화 (F2) 오케스트레이터 테스트.

한 사람이 공개 채널에서 봇 답변을 독점하면 LLM 호출 전에 winding-down 안내 1회 후 잠깐
침묵한다. 단, 버퍼에 다른 '사람'이 있을 때만 발동하고(1:1 제외), 최근 창의 답변 수·비중이
임계 미만이면 평소대로 답한다.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from dcm.i18n import t
from dcm.orchestrator import _MONOPOLY_MIN_REPLIES, Orchestrator
from dcm.platform.base import BufferedMessage, IncomingMessage

_MONOPOLY_REPLY = t("orchestrator.monopoly_reply")


def _llm(reply: str = "응답이야") -> MagicMock:
    m = MagicMock()
    m.complete = AsyncMock(return_value=(reply, False))
    return m


def _orch(llm: MagicMock) -> Orchestrator:
    tmp = Path(tempfile.mkdtemp())
    persona = tmp / "persona.md"
    persona.write_text("너는 지우. 반말로 답해.", encoding="utf-8")
    return Orchestrator(llm=llm, persona_path=persona, bot_name="지우", max_input_chars=4000)


def _inc(author_id="u1", author_name="유저1", channel_id="c1", text="안녕"):
    return IncomingMessage(
        channel_id=channel_id,
        author_id=author_id,
        author_name=author_name,
        content=text,
        guild_id=1,
    )


def _seed(orch: Orchestrator, channel_id: str, authors: list[str]) -> None:
    """채널 봇답변 이력을 (author_id, now) 로 시드한다(윈도 안, 즉시 판정 가능)."""
    now = time.monotonic()
    orch._chan_replies[channel_id] = deque((a, now) for a in authors)


def _others() -> list[BufferedMessage]:
    """버퍼에 '다른 사람' 1명 포함 (피로 유발 대상 존재)."""
    return [BufferedMessage(author_name="딴사람", content="ㅎㅇ", is_bot=False)]


def run(coro):
    return asyncio.run(coro)


def test_monopoly_winddown_when_one_user_dominates():
    llm = _llm()
    orch = _orch(llm)
    _seed(orch, "c1", ["u1"] * _MONOPOLY_MIN_REPLIES)  # 최근 답변 전부 u1 → 독점
    reply = run(orch.handle(_inc(), _others()))
    assert reply == _MONOPOLY_REPLY
    assert llm.complete.await_count == 0  # winding-down → LLM 호출 skip


def test_no_winddown_in_solo_1on1():
    llm = _llm()
    orch = _orch(llm)
    _seed(orch, "c1", ["u1"] * _MONOPOLY_MIN_REPLIES)
    # 버퍼에 다른 사람 없음(자기 자신/봇만) → 피로 대상 없음 → 발동 안 함
    buffer = [
        BufferedMessage(author_name="유저1", content="x", is_bot=False),
        BufferedMessage(author_name="지우", content="y", is_bot=True),
    ]
    reply = run(orch.handle(_inc(), buffer))
    assert reply == "응답이야"
    assert llm.complete.await_count == 1


def test_no_winddown_when_share_below_threshold():
    llm = _llm()
    orch = _orch(llm)
    # 6개 중 u1은 3개(50% < 70%) → 공유된 대화, 독점 아님
    _seed(orch, "c1", ["u1", "u2", "u1", "u3", "u1", "u2"])
    reply = run(orch.handle(_inc(), _others()))
    assert reply == "응답이야"
    assert llm.complete.await_count == 1


def test_no_winddown_below_min_replies():
    llm = _llm()
    orch = _orch(llm)
    _seed(orch, "c1", ["u1"] * (_MONOPOLY_MIN_REPLIES - 1))  # 창에 답변 수 부족
    reply = run(orch.handle(_inc(), _others()))
    assert reply == "응답이야"
    assert llm.complete.await_count == 1


def test_mute_silences_followups_after_winddown():
    llm = _llm()
    orch = _orch(llm)
    _seed(orch, "c1", ["u1"] * _MONOPOLY_MIN_REPLIES)
    first = run(orch.handle(_inc(), _others()))
    assert first == _MONOPOLY_REPLY
    # mute 구간 내 후속 호출은 침묵(None) + LLM 미호출 (반복 안내로 다시 소음 내지 않음)
    second = run(orch.handle(_inc(text="야 뭐해"), _others()))
    assert second is None
    assert llm.complete.await_count == 0


def test_normal_reply_records_channel_history():
    llm = _llm()
    orch = _orch(llm)
    # 이력 없음 → 평소 답변 + 채널 이력 1건 기록
    reply = run(orch.handle(_inc(), _others()))
    assert reply == "응답이야"
    assert len(orch._chan_replies["c1"]) == 1
    assert orch._chan_replies["c1"][0][0] == "u1"
