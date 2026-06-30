"""닫힌 동사셋 위 NL 라우터 (ralplan S3).

LLM을 단일 tool_use 호출로 강제하여 자유형 생성 없이 verb+params를 추출한다.
모든 특권 dispatch는 이 모듈 안 단일 chokepoint(route())를 통과한다.
discord import 없음 — platform/base.AuthContext만 사용.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..platform.base import AuthContext, is_admin

if TYPE_CHECKING:
    from ..llm import LLMClient
    from ..service.guild_admin import GuildAdminService

log = logging.getLogger(__name__)

# GuildAdminService의 실제 verb 집합 + 폴백 센티넬.
VERBS: list[str] = [
    "create_category",
    "create_channel",
    "edit_channel",
    "delete_channel",
    "create_role",
    "assign_role",
    "remove_role",
    "set_role_permissions",
    "create_project",
    "kick",
    "ban",
    "timeout",
    "purge",
    "none",
]

# anthropic tool 정의 — name은 반드시 'dispatch' (tool_choice로 강제).
DISPATCH_TOOL: dict = {
    "name": "dispatch",
    "description": (
        "사용자의 자연어 메시지를 파싱해 Discord 서버 관리 작업으로 매핑한다. "
        "명확한 관리 의도가 없거나 불확실하면 verb를 'none'으로 설정한다."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verb": {
                "type": "string",
                "enum": VERBS,
                "description": (
                    "실행할 관리 작업. 해당 없으면 'none'. "
                    "가능한 값: create_category, create_channel, edit_channel, "
                    "delete_channel, create_role, assign_role, remove_role, "
                    "set_role_permissions, create_project, kick, ban, timeout, purge, none."
                ),
            },
            "params": {
                "type": "object",
                "description": "verb 실행에 필요한 매개변수. verb에 따라 필드가 다름.",
            },
        },
        "required": ["verb"],
    },
}

# 파괴적 verb: NL 경로에서 서비스 호출 없이 확인 안내만 반환 (S3 단순 처리; S5에서 정교화 예정).
_DESTRUCTIVE_VERBS: frozenset[str] = frozenset({"delete_channel", "create_project"})

# verb별 서비스 호출 전 필수 param 목록.
_REQUIRED_PARAMS: dict[str, list[str]] = {
    "create_category": ["name"],
    "create_channel": ["name", "kind"],
    "edit_channel": ["channel_id"],
    "create_role": ["name"],
    "assign_role": ["user_id", "role_id"],
    "remove_role": ["user_id", "role_id"],
    "set_role_permissions": ["role_id", "permissions"],
    "kick": ["user_id"],
    "ban": ["user_id"],
    "timeout": ["user_id", "duration"],
    "purge": ["channel_id", "count"],
}

# 모델이 값을 모를 때 넣는 placeholder/빈 값 — 누락으로 취급해 엉뚱한 이름 생성 방지.
_PLACEHOLDER_VALUES: frozenset[str] = frozenset(
    {"", "<unknown>", "unknown", "none", "null", "n/a", "미정", "없음", "이름", "제목"}
)

_DISPATCH_SYSTEM = (
    "당신은 Discord 서버 관리 명령 파서입니다. "
    "사용자의 메시지를 분석해 dispatch 도구 하나만 호출하세요. "
    "지원 작업: create_category, create_channel, edit_channel, delete_channel, "
    "create_role, assign_role, remove_role, set_role_permissions, create_project, "
    "kick, ban, timeout, purge. "
    "서버 관리 의도가 없으면 verb='none'을 반환하세요. "
    "params에는 작업에 필요한 값만 넣고 없는 값은 추측하지 마세요. "
    "사용자가 이름 등 필수 값을 명시하지 않았으면 그 필드를 아예 넣지 마세요(빈 문자열이나 "
    "<UNKNOWN> 같은 placeholder 금지)."
)


class NLRouter:
    """닫힌 동사셋 위 얕은 NL 라우터 — 단일 특권 dispatch chokepoint.

    route()가 not None을 반환하면 오케스트레이터는 그 결과를 최종 응답으로 사용하고,
    None을 반환하면 페르소나 chat 경로로 폴백한다.
    """

    def __init__(
        self,
        llm: LLMClient,
        service: GuildAdminService,
        admin_role_id: int,
        guild_id: int,
    ) -> None:
        self._llm = llm
        self._service = service
        self._admin_role_id = admin_role_id
        self._guild_id = guild_id

    async def route(self, auth: AuthContext, user_text: str) -> str | None:
        """NL 텍스트를 파싱해 관리 명령 실행 결과를 반환하거나, 폴백 시 None 반환.

        모든 특권 verb는 이 메서드 내 단일 is_admin 검사를 통과해야 한다.
        """
        extracted = await self._llm.extract_dispatch(
            _DISPATCH_SYSTEM,
            user_text,
            tool=DISPATCH_TOOL,
        )
        if extracted is None:
            return None

        verb: str = extracted.get("verb") or "none"
        if verb == "none" or verb not in VERBS:
            return None

        # ── 단일 특권 dispatch chokepoint ──────────────────────────────────
        if not is_admin(auth.role_ids, self._admin_role_id, auth.is_owner):
            return "⛔ 이 명령은 관리자만 사용할 수 있어."

        params: dict = extracted.get("params") or {}

        # 파괴적 작업: 서비스 호출 없이 확인 안내 반환 (NL 경로 직접 실행 불가).
        if verb in _DESTRUCTIVE_VERBS:
            return (
                f"⚠️ '{verb}' 작업은 파괴적이라 NL 경로에서 직접 실행할 수 없어. "
                "슬래시 커맨드로 확인 토큰을 발급받아 진행해줘."
            )

        # 필수 param 검증 — 부족하면 추측 없이 확인 요청.
        required = _REQUIRED_PARAMS.get(verb, [])
        missing = [
            k
            for k in required
            if k not in params
            or params[k] is None
            or (isinstance(params[k], str) and params[k].strip().lower() in _PLACEHOLDER_VALUES)
        ]
        if missing:
            return f"❓ '{verb}' 실행에 필요한 정보가 부족해: {', '.join(missing)}. 다시 알려줘."

        return await self._dispatch(verb, auth, params)

    async def _dispatch(self, verb: str, auth: AuthContext, params: dict) -> str:
        """verb를 GuildAdminService 메서드로 매핑해 호출하고 결과를 한국어로 반환."""
        common = dict(
            guild_id=self._guild_id,
            actor_name=auth.author_name,
            actor_id=auth.author_id,
        )
        try:
            if verb == "create_category":
                result = await self._service.create_category(**common, name=params["name"])
            elif verb == "create_channel":
                result = await self._service.create_channel(
                    **common,
                    name=params["name"],
                    kind=params["kind"],
                    category_id=params.get("category_id"),
                )
            elif verb == "edit_channel":
                result = await self._service.edit_channel(
                    **common,
                    channel_id=params["channel_id"],
                    name=params.get("name"),
                    category_id=params.get("category_id"),
                )
            elif verb == "create_role":
                result = await self._service.create_role(
                    **common,
                    name=params["name"],
                    permissions=params.get("permissions", 0),
                )
            elif verb == "assign_role":
                result = await self._service.assign_role(
                    **common,
                    user_id=params["user_id"],
                    role_id=params["role_id"],
                )
            elif verb == "remove_role":
                result = await self._service.remove_role(
                    **common,
                    user_id=params["user_id"],
                    role_id=params["role_id"],
                )
            elif verb == "set_role_permissions":
                result = await self._service.set_role_permissions(
                    **common,
                    role_id=params["role_id"],
                    permissions=params["permissions"],
                )
            elif verb == "kick":
                result = await self._service.kick_member(**common, user_id=params["user_id"])
            elif verb == "ban":
                result = await self._service.ban_member(**common, user_id=params["user_id"])
            elif verb == "timeout":
                result = await self._service.timeout_member(
                    **common,
                    user_id=params["user_id"],
                    duration_seconds=int(params["duration"]),
                )
            elif verb == "purge":
                result = await self._service.purge_messages(
                    **common,
                    channel_id=params["channel_id"],
                    count=int(params["count"]),
                )
            else:
                log.warning("NLRouter: 알 수 없는 verb=%s", verb)
                return f"❓ 알 수 없는 작업: {verb}"
        except Exception:
            log.exception("NLRouter dispatch 오류 (verb=%s)", verb)
            return "⚠️ 명령 실행 중 오류가 발생했어."

        # 서비스 결과 변환
        if result.needs_confirmation:
            return (
                f"⚠️ '{verb}' 작업은 확인이 필요해 (고위험). "
                "슬래시 커맨드로 확인 토큰을 발급받아 진행해줘."
            )
        if result.ok:
            return f"✅ {result.detail}"
        return f"❌ {result.detail}"
