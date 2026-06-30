"""tests/test_web_isolation.py — S4 web_search 격리 오프라인 테스트.

(a) complete() 반환이 (str, bool) 튜플이고 web_search=False면 web_used False.
(b) orchestrator: complete가 web_used=True 반환하는 턴은 ingest 미호출;
    web_used=False 턴은 ingest 호출.
(c) web_search=True여도 도구 미사용 응답이면 web_used=False.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from dcm.llm import LLMClient
from dcm.orchestrator import Orchestrator
from dcm.platform.base import BufferedMessage, IncomingMessage


# ─────────────────────────── 공통 헬퍼 ──────────────────────────────

def _incoming(author_id: str = "u1", text: str = "안녕") -> IncomingMessage:
    return IncomingMessage(
        channel_id="ch1",
        author_id=author_id,
        author_name="테스트유저",
        content=text,
        role_ids=frozenset(),
    )


def _build_llm(label: str = "key1") -> tuple[LLMClient, MagicMock]:
    """LLMClient 인스턴스와 그 내부 fake anthropic client를 반환."""
    cred = MagicMock()
    cred.label = label

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
    llm._clients = {label: fake_client}
    return llm, fake_client


def _orchestrator(llm: object, ingest=None, tmp_path: Path = None) -> Orchestrator:
    """테스트용 Orchestrator — tmp_path 또는 임시 디렉터리의 persona.md 사용."""
    if tmp_path is None:
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
    persona = tmp_path / "persona.md"
    if not persona.exists():
        persona.write_text("테스트 페르소나", encoding="utf-8")
    return Orchestrator(
        llm=llm,
        persona_path=persona,
        bot_name="지우",
        max_input_chars=4000,
        ingest=ingest,
    )


def _fake_ingest() -> MagicMock:
    mg = MagicMock()
    mg.ingest = AsyncMock()
    return mg


def run(coro):
    return asyncio.run(coro)


# ────────────────── (a) complete() 시그니처 검증 ─────────────────────

def test_complete_returns_tuple():
    """complete() 반환값이 (str, bool) 튜플이어야 함."""
    llm, _ = _build_llm()
    result = run(llm.complete("sys", [{"role": "user", "content": "hi"}]))
    assert isinstance(result, tuple), f"tuple이어야 하는데 {type(result)}"
    assert len(result) == 2
    text, web_used = result
    assert isinstance(text, str)
    assert isinstance(web_used, bool)


def test_complete_web_search_false_returns_web_used_false():
    """web_search=False(기본)이면 web_used는 반드시 False."""
    llm, _ = _build_llm()
    _, web_used = run(llm.complete("sys", [{"role": "user", "content": "hi"}], web_search=False))
    assert web_used is False


def test_complete_web_search_false_no_tools_in_api_call():
    """web_search=False면 API 호출에 tools 파라미터가 포함되지 않아야 함."""
    llm, fake_client = _build_llm()
    run(llm.complete("sys", [{"role": "user", "content": "hi"}], web_search=False))
    call_kwargs = fake_client.messages.create.call_args.kwargs
    assert "tools" not in call_kwargs, "web_search=False면 tools 없어야 함"


def test_complete_web_search_true_attaches_tools():
    """web_search=True면 API 호출에 web_search 도구가 첨부되어야 함."""
    llm, fake_client = _build_llm()
    run(llm.complete("sys", [{"role": "user", "content": "hi"}], web_search=True))
    call_kwargs = fake_client.messages.create.call_args.kwargs
    assert "tools" in call_kwargs, "web_search=True면 tools가 있어야 함"
    assert any(t.get("name") == "web_search" for t in call_kwargs["tools"])


# ────────────────── (b) orchestrator 격리 검증 ────────────────────────

def test_orchestrator_web_used_true_skips_ingest(tmp_path):
    """complete가 (text, web_used=True)를 반환하면 ingest가 호출되지 않아야 함."""
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=("웹 기반 답변", True))

    ingest = _fake_ingest()
    orc = _orchestrator(llm, ingest=ingest, tmp_path=tmp_path)

    run(orc.handle(_incoming(), []))

    # create_task가 실행될 기회를 충분히 줌
    async def _drain():
        for _ in range(3):
            await asyncio.sleep(0)

    run(_drain())
    ingest.ingest.assert_not_called()


def test_orchestrator_web_used_false_calls_ingest(tmp_path):
    """complete가 (text, web_used=False)를 반환하면 ingest가 호출되어야 함."""
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=("일반 답변", False))

    ingest = _fake_ingest()
    orc = _orchestrator(llm, ingest=ingest, tmp_path=tmp_path)

    async def _run_and_drain():
        await orc.handle(_incoming(), [])
        for _ in range(3):
            await asyncio.sleep(0)

    run(_run_and_drain())
    ingest.ingest.assert_called_once()


# ────────────────── (c) 도구 미사용 응답 → web_used=False ──────────────────

def test_complete_web_search_true_but_no_tool_blocks_gives_web_used_false():
    """web_search=True여도 응답 블록에 server_tool_use/web_search_tool_result 없으면 web_used=False."""
    llm, _ = _build_llm()  # fake_block.type == "text" — tool 블록 없음
    _, web_used = run(
        llm.complete("sys", [{"role": "user", "content": "검색해줘"}], web_search=True)
    )
    assert web_used is False


def test_complete_web_search_true_with_server_tool_use_block_gives_web_used_true():
    """응답에 server_tool_use 블록이 있으면 web_used=True."""
    cred = MagicMock()
    cred.label = "key1"

    tool_block = MagicMock()
    tool_block.type = "server_tool_use"
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "검색 결과 기반 답변"

    fake_resp = MagicMock()
    fake_resp.content = [tool_block, text_block]

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_resp)

    llm = LLMClient.__new__(LLMClient)
    llm._creds = [cred]
    llm._model = "claude-3-5-haiku-20241022"
    llm._max_tokens = 500
    llm._clients = {"key1": fake_client}

    text, web_used = run(
        llm.complete("sys", [{"role": "user", "content": "뭔가 검색해줘"}], web_search=True)
    )
    assert web_used is True
    assert text == "검색 결과 기반 답변"


def test_system_prompt_advertises_management_capability(tmp_path):
    """시스템 프롬프트가 서버 관리 능력을 인지시켜 페르소나의 거짓 거절을 막는다 (회귀 가드)."""
    orch = _orchestrator(object(), tmp_path=tmp_path)
    sp = orch._system_prompt()
    assert "카테고리" in sp and "역할" in sp and "모더레이션" in sp
    assert "할 수 있어" in sp  # 능력 긍정
    assert "지어내" in sp  # 실행 안 했는데 성공 지어내기 금지 가드
