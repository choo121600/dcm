"""G003 e2e: 웹 검색 신뢰 게이팅(soft-degrade) — orchestrator 통합."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dcm.leveling.scoring import utc_day
from dcm.leveling.service import LevelingService
from dcm.leveling.store import LevelingStore
from dcm.orchestrator import Orchestrator
from dcm.platform.base import IncomingMessage


def _persona(tmp: str) -> str:
    path = os.path.join(tmp, "persona.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("너는 지우. 친근한 반말로 대답한다.")
    return path


def _incoming(guild_id: int = 1, author_id: str = "42", text: str = "오늘 날씨 검색해줘"):
    return IncomingMessage(
        channel_id="c1",
        author_id=author_id,
        author_name="춘식",
        content=text,
        guild_id=guild_id,
    )


def _service(tmp: str):
    store = LevelingStore(os.path.join(tmp, "leveling.db"))
    return LevelingService(store), store


def _orch(tmp: str, leveling, web_used: bool):
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=("응답이야", web_used))
    orch = Orchestrator(
        llm=llm,
        persona_path=Path(_persona(tmp)),
        bot_name="지우",
        max_input_chars=4000,
        leveling=leveling,
    )
    return orch, llm


def test_quota_limit_derived_from_level():
    assert LevelingService._quota_limit("web", 0) == 20
    assert LevelingService._quota_limit("web", 5) == 50
    assert LevelingService._quota_limit("web", 12) == 100
    assert LevelingService._quota_limit("llm", 0) == 100
    assert LevelingService._quota_limit("llm", 5) == 300
    assert LevelingService._quota_limit("llm", 99) == 600


def test_quota_check_and_record_usage_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            day = utc_day(time.time())
            allowed, remaining = svc.quota_check("g", "u", "web", day=day)
            assert allowed is True and remaining == 20  # lvl0 web 한도 20
            for _ in range(20):
                svc.record_usage("g", "u", "web", day=day)
            allowed, remaining = svc.quota_check("g", "u", "web", day=day)
            assert allowed is False and remaining == 0
            # llm 은 독립 카운터
            assert svc.quota_check("g", "u", "llm", day=day)[0] is True
        finally:
            store.close()


def test_web_over_limit_soft_degrades_to_no_web():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            day = utc_day(time.time())
            for _ in range(20):  # lvl0 한도(20) 소진
                store.incr_daily_usage("1", "42", day, "web")
            orch, llm = _orch(tmp, svc, web_used=False)
            reply = asyncio.run(orch.handle(_incoming(), []))
            assert reply == "응답이야"  # 침묵/치환 아님 — 평소대로 답변
            assert llm.complete.await_args.kwargs["web_search"] is False  # web 강등
        finally:
            store.close()


def test_web_within_limit_uses_web_and_records_usage():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            orch, llm = _orch(tmp, svc, web_used=True)
            asyncio.run(orch.handle(_incoming(), []))
            assert llm.complete.await_args.kwargs["web_search"] is True
            day = utc_day(time.time())
            assert store.get_daily_usage("1", "42", day, "web") == 1  # 실제 사용 → 기록
        finally:
            store.close()


def test_web_used_false_does_not_record_usage():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            orch, llm = _orch(tmp, svc, web_used=False)
            asyncio.run(orch.handle(_incoming(), []))
            day = utc_day(time.time())
            assert store.get_daily_usage("1", "42", day, "web") == 0  # 미사용 → 미기록
        finally:
            store.close()


def test_web_gating_is_guild_isolated():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            day = utc_day(time.time())
            for _ in range(20):  # g1 만 한도 소진
                store.incr_daily_usage("1", "42", day, "web")
            orch, llm = _orch(tmp, svc, web_used=True)
            asyncio.run(orch.handle(_incoming(guild_id=2), []))  # g2 는 한도 내
            assert llm.complete.await_args.kwargs["web_search"] is True  # g2 영향 없음
        finally:
            store.close()
