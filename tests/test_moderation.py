"""tests/test_moderation.py — S5 모더레이션 오프라인 테스트.

GuildAdmin은 FakeGuildAdmin, LLM은 FakeLLM, GuildAdminService는 실제 또는 FakeService로 대체.
실제 네트워크/Discord API 호출 없음.
"""
from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from dcm.agent.router import VERBS, NLRouter
from dcm.platform.base import AuthContext
from dcm.service.guild_admin import (
    GuildAdminService,
    PendingConfirmations,
    OpResult,
    RISK_HIGH,
    RISK_LOW,
)

# ────────────────────────────────── 상수 ──────────────────────────────────
ADMIN_ROLE = 999
GUILD_ID = 12345
NON_ADMIN_AUTH = AuthContext(author_id="user1", author_name="일반유저", role_ids=frozenset(), guild_id=GUILD_ID, admin_role_id=ADMIN_ROLE)
ADMIN_AUTH = AuthContext(
    author_id="admin1", author_name="관리자", role_ids=frozenset({ADMIN_ROLE}), guild_id=GUILD_ID, admin_role_id=ADMIN_ROLE
)


def run(coro):
    return asyncio.run(coro)


# ──────────────────────────────── 테스트 더블 ──────────────────────────────


class FakeGuildAdmin:
    """GuildAdmin 프로토콜의 모더레이션 primitive를 기록하는 최소 스텁."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def kick_member(self, guild_id: int, user_id: int, *, reason: str) -> None:
        self.calls.append(("kick_member", guild_id, user_id, reason))

    async def ban_member(self, guild_id: int, user_id: int, *, reason: str) -> None:
        self.calls.append(("ban_member", guild_id, user_id, reason))

    async def timeout_member(
        self, guild_id: int, user_id: int, duration_seconds: int, *, reason: str
    ) -> None:
        self.calls.append(("timeout_member", guild_id, user_id, duration_seconds, reason))

    async def purge_messages(
        self, guild_id: int, channel_id: int, count: int, *, reason: str
    ) -> int:
        self.calls.append(("purge_messages", guild_id, channel_id, count, reason))
        return count  # 삭제됐다고 가정

    # 비모더레이션 메서드 — 호출되면 안 됨
    async def create_category(self, *a, **kw):
        raise AssertionError("unexpected call")

    async def create_channel(self, *a, **kw):
        raise AssertionError("unexpected call")

    async def edit_channel(self, *a, **kw):
        raise AssertionError("unexpected call")

    async def delete_channel(self, *a, **kw):
        raise AssertionError("unexpected call")

    async def create_role(self, *a, **kw):
        raise AssertionError("unexpected call")

    async def role_permissions(self, *a, **kw):
        raise AssertionError("unexpected call")

    async def assign_role(self, *a, **kw):
        raise AssertionError("unexpected call")

    async def remove_role(self, *a, **kw):
        raise AssertionError("unexpected call")

    async def set_role_permissions(self, *a, **kw):
        raise AssertionError("unexpected call")

    async def set_channel_role_overwrite(self, *a, **kw):
        raise AssertionError("unexpected call")


def _svc(fake_admin=None):
    admin = fake_admin or FakeGuildAdmin()
    pending = PendingConfirmations()
    return GuildAdminService(admin, pending), admin, pending


# ──────────────────────────── 서비스 단위 테스트 ──────────────────────────────


class TestKickMember:
    """kick_member: confirm 필요, 확인 후 실행, audit 포함."""

    def test_kick_requires_confirmation_without_token(self):
        svc, admin, _ = _svc()
        result = run(
            svc.kick_member(
                guild_id=GUILD_ID,
                actor_name="관리자",
                actor_id=1,
                user_id=777,
            )
        )
        assert result.needs_confirmation is True
        assert result.ok is False
        assert result.confirmation_token is not None
        assert result.risk == RISK_HIGH
        assert admin.calls == []  # primitive 미호출

    def test_kick_executes_with_valid_token(self):
        svc, admin, pending = _svc()
        # 1단계: 토큰 발급
        r1 = run(
            svc.kick_member(guild_id=GUILD_ID, actor_name="관리자", actor_id=1, user_id=777)
        )
        token = r1.confirmation_token
        # 2단계: 토큰으로 실행
        r2 = run(
            svc.kick_member(
                guild_id=GUILD_ID,
                actor_name="관리자",
                actor_id=1,
                user_id=777,
                confirm_token=token,
            )
        )
        assert r2.ok is True
        assert r2.risk == RISK_HIGH
        assert len(admin.calls) == 1
        call = admin.calls[0]
        assert call[0] == "kick_member"
        assert call[2] == 777  # user_id

    def test_kick_invalid_token_rejected(self):
        svc, admin, _ = _svc()
        result = run(
            svc.kick_member(
                guild_id=GUILD_ID,
                actor_name="관리자",
                actor_id=1,
                user_id=777,
                confirm_token="deadbeef00000000",
            )
        )
        assert result.ok is False
        assert admin.calls == []

    def test_kick_audit_reason_contains_actor_and_target(self):
        svc, admin, _ = _svc()
        r1 = run(svc.kick_member(guild_id=GUILD_ID, actor_name="지우", actor_id=42, user_id=777))
        token = r1.confirmation_token
        run(
            svc.kick_member(
                guild_id=GUILD_ID,
                actor_name="지우",
                actor_id=42,
                user_id=777,
                confirm_token=token,
            )
        )
        reason = admin.calls[0][3]
        assert "지우" in reason and "42" in reason and "777" in reason


class TestBanMember:
    """ban_member: confirm 필요, 확인 후 실행."""

    def test_ban_requires_confirmation(self):
        svc, admin, _ = _svc()
        result = run(
            svc.ban_member(guild_id=GUILD_ID, actor_name="관리자", actor_id=1, user_id=888)
        )
        assert result.needs_confirmation is True
        assert result.ok is False
        assert result.risk == RISK_HIGH
        assert admin.calls == []

    def test_ban_executes_with_valid_token(self):
        svc, admin, _ = _svc()
        r1 = run(svc.ban_member(guild_id=GUILD_ID, actor_name="관리자", actor_id=1, user_id=888))
        r2 = run(
            svc.ban_member(
                guild_id=GUILD_ID,
                actor_name="관리자",
                actor_id=1,
                user_id=888,
                confirm_token=r1.confirmation_token,
            )
        )
        assert r2.ok is True
        assert admin.calls[0][0] == "ban_member"
        assert admin.calls[0][2] == 888

    def test_ban_token_single_use(self):
        """토큰은 1회만 소비 가능 — 재사용 시 거부."""
        svc, admin, _ = _svc()
        r1 = run(svc.ban_member(guild_id=GUILD_ID, actor_name="관리자", actor_id=1, user_id=888))
        token = r1.confirmation_token
        run(
            svc.ban_member(
                guild_id=GUILD_ID, actor_name="관리자", actor_id=1, user_id=888, confirm_token=token
            )
        )
        # 두 번째 소비 — 반드시 거부
        r3 = run(
            svc.ban_member(
                guild_id=GUILD_ID, actor_name="관리자", actor_id=1, user_id=888, confirm_token=token
            )
        )
        assert r3.ok is False
        assert len(admin.calls) == 1  # 한 번만 실행


class TestTimeoutMember:
    """timeout_member: 즉시 실행, confirm 불필요, 저위험."""

    def test_timeout_immediate_no_confirmation(self):
        svc, admin, _ = _svc()
        result = run(
            svc.timeout_member(
                guild_id=GUILD_ID,
                actor_name="관리자",
                actor_id=1,
                user_id=555,
                duration_seconds=300,
            )
        )
        assert result.ok is True
        assert result.needs_confirmation is False
        assert result.risk == RISK_LOW
        assert len(admin.calls) == 1
        call = admin.calls[0]
        assert call[0] == "timeout_member"
        assert call[2] == 555   # user_id
        assert call[3] == 300   # duration_seconds

    def test_timeout_audit_reason_includes_duration(self):
        svc, admin, _ = _svc()
        run(
            svc.timeout_member(
                guild_id=GUILD_ID,
                actor_name="지우",
                actor_id=42,
                user_id=555,
                duration_seconds=600,
            )
        )
        reason = admin.calls[0][4]
        assert "600" in reason and "지우" in reason


class TestPurgeMessages:
    """purge_messages: count > 100 하드 거부; count ≤ 100은 confirm 필요 → 실행."""

    def test_purge_over_100_hard_reject(self):
        svc, admin, _ = _svc()
        result = run(
            svc.purge_messages(
                guild_id=GUILD_ID,
                actor_name="관리자",
                actor_id=1,
                channel_id=111,
                count=101,
            )
        )
        assert result.ok is False
        assert result.needs_confirmation is False
        assert "100" in result.detail
        assert admin.calls == []

    def test_purge_exactly_100_not_hard_rejected(self):
        """count == 100은 하드 거부 아님 — confirm 경로 진입."""
        svc, admin, _ = _svc()
        result = run(
            svc.purge_messages(
                guild_id=GUILD_ID,
                actor_name="관리자",
                actor_id=1,
                channel_id=111,
                count=100,
            )
        )
        # 하드 거부가 아닌 confirm 필요 응답
        assert result.needs_confirmation is True
        assert admin.calls == []

    def test_purge_requires_confirmation_within_limit(self):
        svc, admin, _ = _svc()
        result = run(
            svc.purge_messages(
                guild_id=GUILD_ID,
                actor_name="관리자",
                actor_id=1,
                channel_id=111,
                count=10,
            )
        )
        assert result.needs_confirmation is True
        assert result.ok is False
        assert result.risk == RISK_HIGH
        assert admin.calls == []

    def test_purge_executes_with_valid_token(self):
        svc, admin, _ = _svc()
        r1 = run(
            svc.purge_messages(
                guild_id=GUILD_ID, actor_name="관리자", actor_id=1, channel_id=111, count=10
            )
        )
        r2 = run(
            svc.purge_messages(
                guild_id=GUILD_ID,
                actor_name="관리자",
                actor_id=1,
                channel_id=111,
                count=10,
                confirm_token=r1.confirmation_token,
            )
        )
        assert r2.ok is True
        assert len(admin.calls) == 1
        call = admin.calls[0]
        assert call[0] == "purge_messages"
        assert call[2] == 111  # channel_id
        assert call[3] == 10   # count

    def test_purge_audit_reason_contains_count_and_channel(self):
        svc, admin, _ = _svc()
        r1 = run(
            svc.purge_messages(
                guild_id=GUILD_ID, actor_name="지우", actor_id=42, channel_id=999, count=5
            )
        )
        run(
            svc.purge_messages(
                guild_id=GUILD_ID,
                actor_name="지우",
                actor_id=42,
                channel_id=999,
                count=5,
                confirm_token=r1.confirmation_token,
            )
        )
        reason = admin.calls[0][4]
        assert "5" in reason and "999" in reason and "지우" in reason

    def test_purge_string_count_cast_to_int(self):
        """count 파라미터를 문자열로 전달해도 정상 처리."""
        svc, admin, _ = _svc()
        result = run(
            svc.purge_messages(
                guild_id=GUILD_ID, actor_name="관리자", actor_id=1, channel_id=111, count="50"
            )
        )
        assert result.needs_confirmation is True  # 50 ≤ 100, confirm 경로


# ─────────────────────────────── 라우터 통합 테스트 ────────────────────────────


@dataclasses.dataclass
class FakeOpResult:
    ok: bool
    detail: str
    needs_confirmation: bool = False
    confirmation_token: str | None = None


class FakeService:
    """GuildAdminService 스텁 — 호출 기록 + ok=True 반환."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _record(self, verb: str, **kwargs: Any) -> FakeOpResult:
        self.calls.append({"verb": verb, **kwargs})
        return FakeOpResult(ok=True, detail=f"{verb} 완료")

    async def kick_member(self, **kwargs: Any) -> FakeOpResult:
        return self._record("kick_member", **kwargs)

    async def ban_member(self, **kwargs: Any) -> FakeOpResult:
        return self._record("ban_member", **kwargs)

    async def timeout_member(self, **kwargs: Any) -> FakeOpResult:
        return self._record("timeout_member", **kwargs)

    async def purge_messages(self, **kwargs: Any) -> FakeOpResult:
        return self._record("purge_messages", **kwargs)


class FakeLLM:
    def __init__(self, result: dict | None) -> None:
        self._result = result

    async def extract_dispatch(
        self, system: str, user_text: str, *, tool: dict, model: str | None = None
    ) -> dict | None:
        return self._result


def make_router(llm: FakeLLM, svc) -> NLRouter:
    return NLRouter(llm=llm, service=svc)


class TestModerationVerbsInVerbList:
    """모더레이션 verb가 VERBS 목록에 포함되는지 확인."""

    def test_kick_in_verbs(self):
        assert "kick" in VERBS

    def test_ban_in_verbs(self):
        assert "ban" in VERBS

    def test_timeout_in_verbs(self):
        assert "timeout" in VERBS

    def test_purge_in_verbs(self):
        assert "purge" in VERBS


class TestModerationNonAdminRejected:
    """비관리자가 모더레이션 verb를 요청하면 단일 chokepoint에서 거부."""

    @pytest.mark.parametrize("verb,params", [
        ("kick", {"user_id": 222}),
        ("ban", {"user_id": 222}),
        ("timeout", {"user_id": 222, "duration": 60}),
        ("purge", {"channel_id": 111, "count": 5}),
    ])
    def test_non_admin_rejected(self, verb: str, params: dict):
        llm = FakeLLM({"verb": verb, "params": params})
        svc = FakeService()
        result = run(make_router(llm, svc).route(NON_ADMIN_AUTH, f"{verb} 해줘"))
        assert result is not None
        assert "관리자" in result or "⛔" in result
        assert svc.calls == [], f"{verb}: 비관리자인데 서비스 호출됨"


class TestModerationRouterDispatch:
    """관리자 + 올바른 params → 서비스 모더레이션 메서드 호출."""

    def test_kick_routes_to_kick_member(self):
        llm = FakeLLM({"verb": "kick", "params": {"user_id": 777}})
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "777 추방해"))
        assert len(svc.calls) == 1
        assert svc.calls[0]["verb"] == "kick_member"
        assert svc.calls[0]["user_id"] == 777
        assert svc.calls[0]["guild_id"] == GUILD_ID
        assert "✅" in result

    def test_ban_routes_to_ban_member(self):
        llm = FakeLLM({"verb": "ban", "params": {"user_id": 888}})
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "888 밴해"))
        assert len(svc.calls) == 1
        assert svc.calls[0]["verb"] == "ban_member"
        assert svc.calls[0]["user_id"] == 888
        assert "✅" in result

    def test_timeout_routes_to_timeout_member_with_duration(self):
        llm = FakeLLM({"verb": "timeout", "params": {"user_id": 555, "duration": 300}})
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "555 타임아웃"))
        assert len(svc.calls) == 1
        assert svc.calls[0]["verb"] == "timeout_member"
        assert svc.calls[0]["user_id"] == 555
        assert svc.calls[0]["duration_seconds"] == 300
        assert "✅" in result

    def test_purge_routes_to_purge_messages(self):
        llm = FakeLLM({"verb": "purge", "params": {"channel_id": 111, "count": 10}})
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "111 채널 메시지 삭제"))
        assert len(svc.calls) == 1
        assert svc.calls[0]["verb"] == "purge_messages"
        assert svc.calls[0]["channel_id"] == 111
        assert svc.calls[0]["count"] == 10
        assert "✅" in result

    def test_actor_info_forwarded_to_service(self):
        """actor_name, actor_id가 서비스에 올바르게 전달된다."""
        llm = FakeLLM({"verb": "kick", "params": {"user_id": 777}})
        svc = FakeService()
        run(make_router(llm, svc).route(ADMIN_AUTH, "추방"))
        assert svc.calls[0]["actor_name"] == ADMIN_AUTH.author_name
        assert svc.calls[0]["actor_id"] == ADMIN_AUTH.author_id


class TestModerationRouterNeedsConfirmation:
    """서비스가 needs_confirmation=True를 반환하면 라우터가 확인 안내를 반환."""

    def test_kick_needs_confirmation_from_service_returns_advisory(self):
        class ConfirmSvc(FakeService):
            async def kick_member(self, **kwargs: Any) -> FakeOpResult:
                self.calls.append({"verb": "kick_member", **kwargs})
                return FakeOpResult(ok=False, detail="확인 필요", needs_confirmation=True)

        llm = FakeLLM({"verb": "kick", "params": {"user_id": 777}})
        svc = ConfirmSvc()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "추방해"))
        assert result is not None
        assert "⚠️" in result or "확인" in result
        assert len(svc.calls) == 1

    def test_purge_hard_reject_from_service_returns_error(self):
        """서비스가 ok=False, needs_confirmation=False → 라우터가 ❌ 반환."""
        class HardRejectSvc(FakeService):
            async def purge_messages(self, **kwargs: Any) -> FakeOpResult:
                self.calls.append({"verb": "purge_messages", **kwargs})
                return FakeOpResult(ok=False, detail="상한 초과", needs_confirmation=False)

        llm = FakeLLM({"verb": "purge", "params": {"channel_id": 111, "count": 200}})
        svc = HardRejectSvc()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "200건 삭제"))
        assert result is not None
        assert "❌" in result


class TestModerationRouterMissingParams:
    """필수 param 누락 → 추측 없이 확인 요청, 서비스 미호출."""

    def test_kick_missing_user_id(self):
        llm = FakeLLM({"verb": "kick", "params": {}})
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "추방해"))
        assert "user_id" in result or "❓" in result
        assert svc.calls == []

    def test_ban_missing_user_id(self):
        llm = FakeLLM({"verb": "ban", "params": {}})
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "밴해"))
        assert "user_id" in result or "❓" in result
        assert svc.calls == []

    def test_timeout_missing_duration(self):
        llm = FakeLLM({"verb": "timeout", "params": {"user_id": 555}})
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "타임아웃"))
        assert "duration" in result or "❓" in result
        assert svc.calls == []

    def test_purge_missing_count(self):
        llm = FakeLLM({"verb": "purge", "params": {"channel_id": 111}})
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "삭제"))
        assert "count" in result or "❓" in result
        assert svc.calls == []

    def test_purge_missing_channel_id(self):
        llm = FakeLLM({"verb": "purge", "params": {"count": 10}})
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "삭제"))
        assert "channel_id" in result or "❓" in result
        assert svc.calls == []
