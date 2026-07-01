"""tests/test_router.py — NLRouter 오프라인 테스트 (ralplan S3).

LLM은 FakeLLM, GuildAdminService는 FakeService로 대체.
실제 네트워크/API 호출 없음.
"""
from __future__ import annotations

import asyncio
import dataclasses
import pytest
from typing import Any

from dcm.agent.router import DISPATCH_TOOL, VERBS, NLRouter, _DESTRUCTIVE_VERBS
from dcm.platform.base import AuthContext

# ────────────────────────────────── 상수 ──────────────────────────────────
ADMIN_ROLE = 999
GUILD_ID = 12345
NON_ADMIN_AUTH = AuthContext(author_id="user1", author_name="일반유저", role_ids=frozenset(), guild_id=GUILD_ID, admin_role_id=ADMIN_ROLE)
ADMIN_AUTH = AuthContext(author_id="admin1", author_name="관리자", role_ids=frozenset({ADMIN_ROLE}), guild_id=GUILD_ID, admin_role_id=ADMIN_ROLE)

# 모든 특권 verb (none 제외)
PRIVILEGED_VERBS = [v for v in VERBS if v != "none"]

# 테스트에 쓸 verb별 최소 충분 params
_FULL_PARAMS: dict[str, dict] = {
    "create_category": {"name": "테스트카테고리"},
    "create_channel": {"name": "채널명", "kind": "text"},
    "edit_channel": {"channel_id": 111},
    "delete_channel": {"channel_id": 111},
    "create_role": {"name": "역할명"},
    "assign_role": {"user_id": 222, "role_id": 333},
    "remove_role": {"user_id": 222, "role_id": 333},
    "set_role_permissions": {"role_id": 333, "permissions": 0},
    "create_project": {"name": "프로젝트", "channels": ["general"], "access_role_name": "멤버"},
    "kick": {"user_id": 222},
    "ban": {"user_id": 222},
    "timeout": {"user_id": 222, "duration": 60},
    "purge": {"channel_id": 111, "count": 5},
}


# ──────────────────────────────── 테스트 더블 ──────────────────────────────


class FakeLLM:
    """extract_dispatch가 지정된 dict를 반환하는 LLM 스텁."""

    def __init__(self, result: dict | None) -> None:
        self._result = result
        self.calls: list[dict] = []

    async def extract_dispatch(
        self, system: str, user_text: str, *, tool: dict, model: str | None = None
    ) -> dict | None:
        self.calls.append({"system": system, "user_text": user_text, "tool": tool, "model": model})
        return self._result


@dataclasses.dataclass
class FakeOpResult:
    ok: bool
    detail: str
    needs_confirmation: bool = False
    confirmation_token: str | None = None


class FakeService:
    """GuildAdminService 스텁 — 호출을 기록하고 ok=True FakeOpResult를 반환."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _record(self, verb: str, **kwargs: Any) -> FakeOpResult:
        self.calls.append({"verb": verb, **kwargs})
        return FakeOpResult(ok=True, detail=f"{verb} 완료")

    async def create_category(self, **kwargs: Any) -> FakeOpResult:
        return self._record("create_category", **kwargs)

    async def create_channel(self, **kwargs: Any) -> FakeOpResult:
        return self._record("create_channel", **kwargs)

    async def edit_channel(self, **kwargs: Any) -> FakeOpResult:
        return self._record("edit_channel", **kwargs)

    async def delete_channel(self, **kwargs: Any) -> FakeOpResult:  # 파괴적 — 호출되면 안 됨
        return self._record("delete_channel", **kwargs)

    async def create_role(self, **kwargs: Any) -> FakeOpResult:
        return self._record("create_role", **kwargs)

    async def assign_role(self, **kwargs: Any) -> FakeOpResult:
        return self._record("assign_role", **kwargs)

    async def remove_role(self, **kwargs: Any) -> FakeOpResult:
        return self._record("remove_role", **kwargs)

    async def set_role_permissions(self, **kwargs: Any) -> FakeOpResult:
        return self._record("set_role_permissions", **kwargs)

    async def create_project(self, **kwargs: Any) -> FakeOpResult:  # 파괴적 — 호출되면 안 됨
        return self._record("create_project", **kwargs)

    async def kick_member(self, **kwargs: Any) -> FakeOpResult:
        return self._record("kick", **kwargs)

    async def ban_member(self, **kwargs: Any) -> FakeOpResult:
        return self._record("ban", **kwargs)

    async def timeout_member(self, **kwargs: Any) -> FakeOpResult:
        return self._record("timeout", **kwargs)

    async def purge_messages(self, **kwargs: Any) -> FakeOpResult:
        return self._record("purge", **kwargs)



def make_router(llm: FakeLLM, svc: FakeService) -> NLRouter:
    return NLRouter(llm=llm, service=svc)


def run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────── 테스트 ───────────────────────────────


class TestVerbNone:
    """verb=='none' → route가 None 반환 (페르소나 chat 폴백)."""

    def test_verb_none_returns_none(self):
        llm = FakeLLM({"verb": "none"})
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "안녕하세요"))
        assert result is None
        assert svc.calls == []

    def test_verb_none_non_admin_also_returns_none(self):
        llm = FakeLLM({"verb": "none"})
        svc = FakeService()
        result = run(make_router(llm, svc).route(NON_ADMIN_AUTH, "안녕"))
        assert result is None

    def test_extract_none_returns_none(self):
        """extract_dispatch가 None을 반환하면 route도 None 반환."""
        llm = FakeLLM(None)
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "뭔가 말하는 중"))
        assert result is None
        assert svc.calls == []


class TestAuthzRejection:
    """비관리자가 모든 특권 verb를 요청하면 거부 메시지 반환, 서비스 미호출."""

    @pytest.mark.parametrize("verb", PRIVILEGED_VERBS)
    def test_non_admin_rejected_for_all_privileged_verbs(self, verb: str):
        params = _FULL_PARAMS[verb]
        llm = FakeLLM({"verb": verb, "params": params})
        svc = FakeService()
        result = run(make_router(llm, svc).route(NON_ADMIN_AUTH, "채널 만들어줘"))
        # 거부 메시지 반환
        assert result is not None
        assert "관리자" in result or "⛔" in result
        # 서비스 절대 미호출
        assert svc.calls == [], f"verb={verb}: 서비스가 호출됨 — 인가 chokepoint 우회됨"


class TestDestructiveVerbs:
    """파괴적 verb (delete_channel, create_project): 관리자여도 서비스 미호출, 확인 안내 반환."""

    @pytest.mark.parametrize("verb", sorted(_DESTRUCTIVE_VERBS))
    def test_destructive_verb_returns_advisory_no_service_call(self, verb: str):
        params = _FULL_PARAMS[verb]
        llm = FakeLLM({"verb": verb, "params": params})
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, f"{verb} 실행해줘"))
        assert result is not None
        assert "⚠️" in result or "확인" in result or "슬래시" in result
        assert svc.calls == [], f"verb={verb}: 파괴적 op이 서비스를 직접 호출함"


class TestAdminDispatch:
    """관리자 + 올바른 params → 정확한 FakeService 메서드 호출."""

    NON_DESTRUCTIVE_VERBS = [v for v in PRIVILEGED_VERBS if v not in _DESTRUCTIVE_VERBS]

    @pytest.mark.parametrize("verb", NON_DESTRUCTIVE_VERBS)
    def test_admin_with_full_params_dispatches_to_service(self, verb: str):
        params = _FULL_PARAMS[verb]
        llm = FakeLLM({"verb": verb, "params": params})
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, f"{verb} 실행해"))
        # 서비스 1회 호출, 올바른 verb
        assert len(svc.calls) == 1
        call = svc.calls[0]
        assert call["verb"] == verb
        # guild_id, actor_id, actor_name 전달 확인
        assert call["guild_id"] == GUILD_ID
        assert call["actor_id"] == ADMIN_AUTH.author_id
        assert call["actor_name"] == ADMIN_AUTH.author_name
        # 결과는 성공 응답
        assert result is not None
        assert "✅" in result

    def test_create_category_passes_name(self):
        llm = FakeLLM({"verb": "create_category", "params": {"name": "프로젝트룸"}})
        svc = FakeService()
        run(make_router(llm, svc).route(ADMIN_AUTH, "카테고리 만들어"))
        assert svc.calls[0]["name"] == "프로젝트룸"

    def test_create_channel_passes_name_and_kind(self):
        llm = FakeLLM({"verb": "create_channel", "params": {"name": "공지", "kind": "text"}})
        svc = FakeService()
        run(make_router(llm, svc).route(ADMIN_AUTH, "채널 만들어"))
        call = svc.calls[0]
        assert call["name"] == "공지"
        assert call["kind"] == "text"

    def test_assign_role_passes_user_and_role(self):
        llm = FakeLLM({"verb": "assign_role", "params": {"user_id": 55, "role_id": 77}})
        svc = FakeService()
        run(make_router(llm, svc).route(ADMIN_AUTH, "역할 부여해"))
        call = svc.calls[0]
        assert call["user_id"] == 55
        assert call["role_id"] == 77


class TestMissingParams:
    """필수 params 부족 → 추측 없이 확인 메시지, 서비스 미호출."""

    def test_create_category_missing_name(self):
        llm = FakeLLM({"verb": "create_category", "params": {}})
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "카테고리 만들어"))
        assert result is not None
        assert "name" in result or "❓" in result
        assert svc.calls == []

    def test_create_channel_missing_kind(self):
        llm = FakeLLM({"verb": "create_channel", "params": {"name": "채널"}})
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "채널 만들어"))
        assert "kind" in result or "❓" in result
        assert svc.calls == []

    def test_assign_role_missing_role_id(self):
        llm = FakeLLM({"verb": "assign_role", "params": {"user_id": 10}})
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "역할 줘"))
        assert "role_id" in result or "❓" in result
        assert svc.calls == []

    def test_set_role_permissions_missing_permissions(self):
        llm = FakeLLM({"verb": "set_role_permissions", "params": {"role_id": 5}})
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "권한 설정해"))
        assert "permissions" in result or "❓" in result
        assert svc.calls == []

    def test_params_none_treated_as_empty(self):
        """params 키가 아예 없어도 처리 가능해야 함."""
        llm = FakeLLM({"verb": "create_category"})  # params 키 없음
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "카테고리 만들어"))
        assert "name" in result or "❓" in result
        assert svc.calls == []


class TestNeedsConfirmationFromService:
    """서비스가 needs_confirmation=True를 반환하면 라우터가 확인 안내를 반환."""

    def test_service_needs_confirmation_returns_advisory(self):
        class NeedsConfirmSvc(FakeService):
            async def create_role(self, **kwargs):
                self.calls.append({"verb": "create_role", **kwargs})
                return FakeOpResult(ok=False, detail="확인 필요", needs_confirmation=True)

        llm = FakeLLM({"verb": "create_role", "params": {"name": "관리자역할"}})
        svc = NeedsConfirmSvc()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "역할 만들어"))
        assert result is not None
        assert "⚠️" in result or "확인" in result
        assert len(svc.calls) == 1  # 서비스는 호출됨


class TestDispatchToolShape:
    """DISPATCH_TOOL 구조 불변 검사."""

    def test_tool_name_is_dispatch(self):
        assert DISPATCH_TOOL["name"] == "dispatch"

    def test_verb_enum_matches_verbs_constant(self):
        enum_verbs = DISPATCH_TOOL["input_schema"]["properties"]["verb"]["enum"]
        assert set(enum_verbs) == set(VERBS)

    def test_verb_required(self):
        assert "verb" in DISPATCH_TOOL["input_schema"]["required"]

    def test_none_in_verbs(self):
        assert "none" in VERBS


class TestRealServiceSignatureMatch:
    """회귀 가드(architect S3 LOW): 실제 GuildAdminService로 라우팅해 _dispatch의
    keyword-only 인자가 실제 시그니처와 어긋나면 TypeError로 잡히게 한다.
    (FakeService는 **kwargs라 시그니처 드리프트를 못 잡음.)"""

    def test_dispatch_matches_real_guildadminservice_signature(self):
        from dcm.service.guild_admin import GuildAdminService, PendingConfirmations

        class FakeGuildAdmin:
            """GuildAdmin 프로토콜의 create_category primitive만 구현한 최소 스텁."""

            def __init__(self):
                self.created = []

            async def create_category(self, guild_id, name, *, reason):
                self.created.append((guild_id, name, reason))
                return "900"

        fake_admin = FakeGuildAdmin()
        service = GuildAdminService(fake_admin, PendingConfirmations())
        llm = FakeLLM({"verb": "create_category", "params": {"name": "팀A"}})
        router = NLRouter(llm=llm, service=service)

        result = run(router.route(ADMIN_AUTH, "팀A 카테고리 만들어줘"))

        # 실제 GuildAdminService.create_category(*, guild_id, actor_name, actor_id, name)
        # 경로가 호출됨 → primitive 호출 + 성공 메시지. 시그니처 드리프트면 TypeError.
        assert fake_admin.created and fake_admin.created[0][1] == "팀A"
        assert result is not None and result.startswith("✅")


class TestGuildOwnerBypass:
    """서버 주인(is_owner=True)은 관리 역할이 없어도 chokepoint를 통과한다."""

    OWNER_AUTH = AuthContext(
        author_id="owner1", author_name="서버주인", role_ids=frozenset(), is_owner=True, guild_id=GUILD_ID
    )

    def test_owner_without_admin_role_dispatches(self):
        llm = FakeLLM({"verb": "create_category", "params": {"name": "주인방"}})
        svc = FakeService()
        result = run(make_router(llm, svc).route(self.OWNER_AUTH, "카테고리 만들어"))
        assert len(svc.calls) == 1, "owner was blocked by the authz chokepoint"
        assert svc.calls[0]["verb"] == "create_category"
        assert svc.calls[0]["actor_id"] == "owner1"
        assert result is not None and "✅" in result


class TestPlaceholderNameRejected:
    """모델이 이름을 모를 때 넣는 placeholder/빈 값은 누락으로 처리해 엉뚱한 이름 생성 방지."""

    @pytest.mark.parametrize("bad", ["<UNKNOWN>", "", "   ", "미정", "이름", "unknown"])
    def test_placeholder_name_asks_instead_of_creating(self, bad: str):
        llm = FakeLLM({"verb": "create_category", "params": {"name": bad}})
        svc = FakeService()
        result = run(make_router(llm, svc).route(ADMIN_AUTH, "카테고리 만들어"))
        assert svc.calls == [], f"placeholder {bad!r} 이 채널을 생성함"
        assert result is not None and "부족" in result


def test_missing_guild_id_rejected():
    """guild_id 없는 컨텍스트(이론상 DM)는 verb가 잡혀도 거부 + 서비스 미호출 (fail-closed)."""
    llm = FakeLLM({"verb": "create_category", "params": {"name": "x"}})
    svc = FakeService()
    no_guild = AuthContext(author_id="u", author_name="n", role_ids=frozenset({ADMIN_ROLE}))
    result = run(make_router(llm, svc).route(no_guild, "카테고리 만들어"))
    assert result is not None and ("서버 안에서만" in result or "⛔" in result)
    assert svc.calls == []

def test_dispatch_uses_configured_model():
    """비용 절감: dispatch_model 설정 시 extract_dispatch 에 그 모델(haiku)이 전달된다."""
    llm = FakeLLM({"verb": "none"})
    svc = FakeService()
    router = NLRouter(llm=llm, service=svc, dispatch_model="claude-haiku-x")
    run(router.route(ADMIN_AUTH, "안녕"))
    assert llm.calls and llm.calls[0]["model"] == "claude-haiku-x"
