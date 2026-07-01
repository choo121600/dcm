"""G004: LLM 대화 게이팅(캔드 치환) + 레벨→역할 보상(allow-list 무인 부여)."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dcm.i18n import t
from dcm.leveling.scoring import cum_cost, utc_day
from dcm.leveling.service import DANGEROUS_PERMISSION_NAMES, LevelingService
from dcm.leveling.store import LevelingStore
from dcm.orchestrator import Orchestrator
from dcm.platform.base import IncomingMessage

_LLM_QUOTA_REPLY = t("orchestrator.llm_quota_reply")

# ───────────────────────── 공통 헬퍼 ─────────────────────────

def _service(tmp: str):
    store = LevelingStore(os.path.join(tmp, "leveling.db"))
    return LevelingService(store), store


def _persona(tmp: str) -> str:
    path = os.path.join(tmp, "persona.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("너는 지우.")
    return path


def _incoming(guild_id: int = 1, author_id: str = "42", text: str = "안녕 지우야"):
    return IncomingMessage(
        channel_id="c", author_id=author_id, author_name="춘식",
        content=text, guild_id=guild_id,
    )


def _orch(tmp, leveling, *, web_used=False, router=None):
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=("페르소나 응답", web_used))
    orch = Orchestrator(
        llm=llm, persona_path=Path(_persona(tmp)), bot_name="지우",
        max_input_chars=4000, leveling=leveling, router=router,
    )
    return orch, llm


def _perms(**on):
    p = types.SimpleNamespace()
    for name in DANGEROUS_PERMISSION_NAMES:
        setattr(p, name, False)
    for name in on:
        setattr(p, name, True)
    return p


def _role(role_id=100, position=1, managed=False, **dangerous):
    return types.SimpleNamespace(
        id=role_id, position=position, managed=managed,
        name=f"role{role_id}", permissions=_perms(**dangerous),
    )


def _guild(bot_top=10, default_role=None):
    me = types.SimpleNamespace(top_role=types.SimpleNamespace(position=bot_top))
    return types.SimpleNamespace(me=me, default_role=default_role)


# ───────────────────────── LLM 대화 게이팅 ─────────────────────────

def test_llm_over_limit_returns_canned_reply_without_calling_llm():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            day = utc_day(time.time())
            for _ in range(100):  # lvl0 llm 한도(100) 소진
                store.incr_daily_usage("1", "42", day, "llm")
            orch, llm = _orch(tmp, svc)
            reply = asyncio.run(orch.handle(_incoming(), []))
            assert reply == _LLM_QUOTA_REPLY  # 침묵 아님 — 캔드 '지우' 톤 안내
            assert llm.complete.await_count == 0  # LLM 호출 자체를 skip
        finally:
            store.close()


def test_llm_under_limit_calls_llm_and_records_usage():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            orch, llm = _orch(tmp, svc)
            reply = asyncio.run(orch.handle(_incoming(), []))
            assert reply == "페르소나 응답"
            assert llm.complete.await_count == 1
            day = utc_day(time.time())
            assert store.get_daily_usage("1", "42", day, "llm") == 1
        finally:
            store.close()


def test_llm_gate_does_not_block_router_path():
    # router(특권/명령) 단락은 LLM 게이트 이전 — 한도 초과여도 절대 막히면 안 됨(Non-Goal).
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            day = utc_day(time.time())
            for _ in range(100):  # llm 한도 소진
                store.incr_daily_usage("1", "42", day, "llm")
            router = MagicMock()
            router.route = AsyncMock(return_value="라우터가 처리한 관리 명령 결과")
            orch, llm = _orch(tmp, svc, router=router)
            reply = asyncio.run(orch.handle(_incoming(text="채널 만들어줘"), []))
            assert reply == "라우터가 처리한 관리 명령 결과"  # 게이트에 막히지 않음
            assert llm.complete.await_count == 0  # 페르소나 chat 미진입
        finally:
            store.close()


# ───────────────────────── 역할 보상 allow-list ─────────────────────────

def test_role_grant_ok_accepts_safe_role():
    role = _role(position=1)
    ok, reason = LevelingService._role_grant_ok(role, _guild(bot_top=10), 10)
    assert ok is True and reason == "ok"


def test_role_grant_ok_rejects_managed_and_everyone_and_hierarchy():
    assert LevelingService._role_grant_ok(_role(managed=True), _guild(), 10)[0] is False
    default = _role(role_id=999, position=0)
    g = _guild(bot_top=10, default_role=default)
    assert LevelingService._role_grant_ok(default, g, 10)[0] is False  # @everyone
    # 봇 최상위 역할 이상 위치 → 거부
    assert LevelingService._role_grant_ok(_role(position=10), _guild(), 10)[0] is False
    assert LevelingService._role_grant_ok(_role(position=11), _guild(), 10)[0] is False
    # 봇 top role 없음 → 거부
    assert LevelingService._role_grant_ok(_role(position=1), _guild(), None)[0] is False


def test_role_grant_ok_rejects_each_dangerous_permission():
    for name in DANGEROUS_PERMISSION_NAMES:
        role = _role(position=1, **{name: True})
        ok, reason = LevelingService._role_grant_ok(role, _guild(bot_top=10), 10)
        assert ok is False and reason == f"dangerous:{name}", name


# ───────────────────────── reconcile_roles 무인 부여 ─────────────────────────

def _member(guild, role_lookup, *, member_id="42", roles=None):
    return types.SimpleNamespace(
        id=member_id,
        guild=types.SimpleNamespace(
            id=guild["id"], me=guild["me"], default_role=guild.get("default_role"),
            get_role=lambda rid: role_lookup.get(int(rid)),
        ),
        roles=roles if roles is not None else [],
        add_roles=AsyncMock(),
    )


def _guild_dict(bot_top=10, default_role=None):
    return {"id": "1", "me": types.SimpleNamespace(top_role=types.SimpleNamespace(position=bot_top)),
            "default_role": default_role}


def test_reconcile_grants_safe_role_at_threshold():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.set_role_reward("1", 1, 100)  # 레벨 1 → 역할 100
            store.add_xp("1", "42", 100000, now=1.0)  # 충분히 높은 레벨
            safe = _role(role_id=100, position=1)
            member = _member(_guild_dict(), {100: safe})
            asyncio.run(svc.reconcile_roles(member))
            member.add_roles.assert_awaited_once()
            assert member.add_roles.await_args.args[0] is safe
        finally:
            store.close()


def test_reconcile_rejects_dangerous_role():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.set_role_reward("1", 1, 100)
            store.add_xp("1", "42", 100000, now=1.0)
            danger = _role(role_id=100, position=1, manage_roles=True)
            member = _member(_guild_dict(), {100: danger})
            asyncio.run(svc.reconcile_roles(member))
            member.add_roles.assert_not_awaited()  # 위험 역할 거부
        finally:
            store.close()


def test_reconcile_idempotent_when_already_held():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.set_role_reward("1", 1, 100)
            store.add_xp("1", "42", 100000, now=1.0)
            safe = _role(role_id=100, position=1)
            member = _member(_guild_dict(), {100: safe}, roles=[safe])  # 이미 보유
            asyncio.run(svc.reconcile_roles(member))
            member.add_roles.assert_not_awaited()
        finally:
            store.close()


def test_reconcile_below_threshold_does_not_grant():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.set_role_reward("1", 5, 100)  # 레벨 5 필요
            store.add_xp("1", "42", 50, now=1.0)  # 레벨 0
            safe = _role(role_id=100, position=1)
            member = _member(_guild_dict(), {100: safe})
            asyncio.run(svc.reconcile_roles(member))
            member.add_roles.assert_not_awaited()
        finally:
            store.close()


def test_reconcile_silent_on_forbidden():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.set_role_reward("1", 1, 100)
            store.add_xp("1", "42", 100000, now=1.0)
            safe = _role(role_id=100, position=1)
            member = _member(_guild_dict(), {100: safe})
            member.add_roles = AsyncMock(side_effect=RuntimeError("Forbidden"))
            # 예외 미전파(침묵 degrade)
            asyncio.run(svc.reconcile_roles(member))
        finally:
            store.close()


def test_reconcile_skips_deleted_role():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.set_role_reward("1", 1, 100)
            store.add_xp("1", "42", 100000, now=1.0)
            member = _member(_guild_dict(), {})  # get_role → None(삭제됨)
            asyncio.run(svc.reconcile_roles(member))
            member.add_roles.assert_not_awaited()
        finally:
            store.close()


def test_reconcile_no_rewards_is_noop():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.add_xp("1", "42", 100000, now=1.0)
            member = _member(_guild_dict(), {100: _role()})
            asyncio.run(svc.reconcile_roles(member))
            member.add_roles.assert_not_awaited()
        finally:
            store.close()


def _member_rm(guild, role_lookup, *, roles=None, member_id="42"):
    """remove_roles 까지 가진 멤버(회수 테스트용)."""
    return types.SimpleNamespace(
        id=member_id,
        guild=types.SimpleNamespace(
            id=guild["id"], me=guild["me"], default_role=guild.get("default_role"),
            get_role=lambda rid: role_lookup.get(int(rid)),
        ),
        roles=roles if roles is not None else [],
        add_roles=AsyncMock(),
        remove_roles=AsyncMock(),
    )


def test_reconcile_revokes_reward_role_when_demoted_below_hysteresis():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.set_role_reward("1", 5, 100)  # 레벨 5 → 역할 100
            store.add_xp("1", "42", 50, now=1.0)  # 레벨 0 (< 5 - 1)
            safe = _role(role_id=100, position=1)
            member = _member_rm(_guild_dict(), {100: safe}, roles=[safe])  # 보유 중
            asyncio.run(svc.reconcile_roles(member))
            member.remove_roles.assert_awaited_once()
            assert member.remove_roles.await_args.args[0] is safe
        finally:
            store.close()


def test_reconcile_no_revoke_within_hysteresis_band():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.set_role_reward("1", 5, 100)
            # 레벨 4 (= reward_level - 1): 히스테리시스 밴드 → 회수 안 함(sticky)
            store.add_xp("1", "42", cum_cost(4) + 5, now=1.0)
            safe = _role(role_id=100, position=1)
            member = _member_rm(_guild_dict(), {100: safe}, roles=[safe])
            asyncio.run(svc.reconcile_roles(member))
            member.remove_roles.assert_not_awaited()
        finally:
            store.close()


def test_reconcile_does_not_revoke_without_tier_drop():
    # AC6: 임계 이상이면 회수 없음(이미 보유 → 멱등, 역할 변동 0).
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.set_role_reward("1", 1, 100)
            store.add_xp("1", "42", 100000, now=1.0)  # 레벨 충분
            safe = _role(role_id=100, position=1)
            member = _member_rm(_guild_dict(), {100: safe}, roles=[safe])
            asyncio.run(svc.reconcile_roles(member))
            member.remove_roles.assert_not_awaited()
            member.add_roles.assert_not_awaited()
        finally:
            store.close()


def test_reconcile_does_not_revoke_unsafe_reward_role():
    # 위험권한 역할은 _role_grant_ok 미통과 → 자동 회수 안 함(보존).
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.set_role_reward("1", 5, 100)
            store.add_xp("1", "42", 0, now=1.0)  # 레벨 0
            danger = _role(role_id=100, position=1, manage_roles=True)
            member = _member_rm(_guild_dict(), {100: danger}, roles=[danger])
            asyncio.run(svc.reconcile_roles(member))
            member.remove_roles.assert_not_awaited()
        finally:
            store.close()


# ───────────────────────── 역할 보상 설정 (admin) ─────────────────────────

def test_role_reward_config_crud_and_validation():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            svc.set_role_reward("1", 5, 100)
            svc.set_role_reward("1", 10, 200)
            assert svc.list_role_rewards("1") == [(5, 100), (10, 200)]
            svc.remove_role_reward("1", 5)
            assert svc.list_role_rewards("1") == [(10, 200)]
            # 설정 시점 검증: 안전 역할 통과, 위험 역할 거부
            assert svc.validate_reward_role(_role(position=1), _guild(bot_top=10))[0] is True
            assert svc.validate_reward_role(_role(position=1, administrator=True), _guild(bot_top=10))[0] is False
        finally:
            store.close()


def test_role_grant_ok_fail_closed_on_unenumerated_permission():
    # SAFE 도 명시 DANGEROUS 도 아닌 실제 권한(create_instant_invite)도 fail-closed 로 거부.
    role = _role(position=1, create_instant_invite=True)
    ok, reason = LevelingService._role_grant_ok(role, _guild(bot_top=10), 10)
    assert ok is False
    assert reason.startswith(("unsafe-perm:", "dangerous:"))


def test_role_grant_ok_allows_actual_safe_chat_voice_permissions():
    # 안전 권한(send_messages/connect/add_reactions)을 실제로 가진 장식 역할은 통과.
    role = _role(position=1, send_messages=True, connect=True, add_reactions=True)
    ok, reason = LevelingService._role_grant_ok(role, _guild(bot_top=10), 10)
    assert ok is True and reason == "ok"
