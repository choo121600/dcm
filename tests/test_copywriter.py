"""service/copywriter.py 테스트 — 문구 다듬기(LLM) + fail-open (discord-free)."""
from __future__ import annotations

import asyncio

from dcm.service.copywriter import MAX_POLISHED_CHARS, polish_copy


class _FakeLLM:
    def __init__(self, out=None, raise_=False):
        self.out = out
        self.raise_ = raise_
        self.calls: list = []

    async def complete(self, system, messages, **kw):
        self.calls.append((system, messages))
        if self.raise_:
            raise RuntimeError("boom")
        return (self.out, False)


def _run(coro):
    return asyncio.run(coro)


def test_polish_returns_refined_text():
    llm = _FakeLLM(out="이번 주 토요일, 같이 봐요! 🎉")
    out = _run(polish_copy(llm, bot_name="썩스가재", raw="토욜 모임함 오셈", kind="event", title="여름 OT"))
    assert out == "이번 주 토요일, 같이 봐요! 🎉"
    # 시스템 프롬프트에 봇 이름 + '사실 보존' 규칙, 유저 메시지에 원문/행사명 포함
    system, messages = llm.calls[0]
    assert "썩스가재" in system and "지어내지 마" in system
    user = messages[0]["content"]
    assert "토욜 모임함 오셈" in user and "여름 OT" in user


def test_polish_empty_input_returns_empty():
    llm = _FakeLLM(out="무언가")
    assert _run(polish_copy(llm, bot_name="b", raw="   ")) == ""
    assert llm.calls == []  # 빈 입력이면 LLM 호출 안 함


def test_polish_llm_failure_keeps_original():
    llm = _FakeLLM(raise_=True)
    assert _run(polish_copy(llm, bot_name="b", raw="원문 유지")) == "원문 유지"


def test_polish_empty_response_keeps_original():
    llm = _FakeLLM(out="   ")
    assert _run(polish_copy(llm, bot_name="b", raw="원문2")) == "원문2"


def test_polish_strips_wrapping_quotes():
    llm = _FakeLLM(out='"따옴표 감싼 결과"')
    assert _run(polish_copy(llm, bot_name="b", raw="x")) == "따옴표 감싼 결과"


def test_polish_caps_length():
    llm = _FakeLLM(out="가" * 900)
    assert len(_run(polish_copy(llm, bot_name="b", raw="x"))) == MAX_POLISHED_CHARS
