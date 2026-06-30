"""Platform-agnostic guild-management policy (ralplan S2).

Pure policy with **no discord import** (DIP on the ``GuildAdmin`` protocol in
``platform/base.py``): risk classification, the bot's self-deny / escalation classifier,
confirmation-token carry, and X-Audit-Log-Reason assembly. The adapter renders confirmation
and performs the actual Discord mutations; this module decides *whether* and *how*.

Escalation note (ralplan R9, accepted risk): the bot ceiling is Manage Roles + Manage
Channels, so Discord already refuses to grant perms the bot lacks. The only real escalation
path is creating/granting a role that carries *management* permissions; this module marks
that path HIGH risk so it always requires explicit confirmation, and stamps every mutation
with the human caller for audit attribution.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation only — never import platform/base at runtime (keeps service discord-free)
    from ..platform.base import GuildAdmin

# --- Discord permission bits (stable Discord API constants; kept here so the service
#     layer never has to import discord). See discord.dev/topics/permissions. ---
KICK_MEMBERS = 1 << 1
BAN_MEMBERS = 1 << 2
ADMINISTRATOR = 1 << 3
MANAGE_CHANNELS = 1 << 4
MANAGE_GUILD = 1 << 5
MANAGE_MESSAGES = 1 << 13
MANAGE_NICKNAMES = 1 << 27
MANAGE_ROLES = 1 << 28
MANAGE_WEBHOOKS = 1 << 29
MODERATE_MEMBERS = 1 << 40

# A role carrying ANY of these effectively mints a (near-)admin and is the real escalation
# vector within the bot's ceiling. Granting/creating such a role is HIGH risk (confirm-gated).
MANAGEMENT_PERMISSIONS = (
    ADMINISTRATOR
    | MANAGE_GUILD
    | MANAGE_ROLES
    | MANAGE_CHANNELS
    | MANAGE_WEBHOOKS
    | KICK_MEMBERS
    | BAN_MEMBERS
    | MANAGE_NICKNAMES
    | MODERATE_MEMBERS
)

RISK_LOW = "low"
RISK_HIGH = "high"
AUDIT_REASON_MAX = 512  # Discord X-Audit-Log-Reason hard limit

# Intrinsically destructive/bundled actions are HIGH risk regardless of permissions.
_DESTRUCTIVE_ACTIONS = frozenset({"delete_channel", "create_project", "kick_member", "ban_member", "purge_messages"})
# Role-touching actions become HIGH risk when the role carries management permissions.
_ROLE_ACTIONS = frozenset({"create_role", "set_role_permissions", "assign_role"})


def role_is_dangerous(permissions: int) -> bool:
    """True when a role's permission bitfield includes any management permission."""
    return bool(permissions & MANAGEMENT_PERMISSIONS)


def classify_risk(action_kind: str, *, role_permissions: int | None = None) -> str:
    """Classify an action as RISK_LOW or RISK_HIGH (ralplan S6 guard policy)."""
    if action_kind in _DESTRUCTIVE_ACTIONS:
        return RISK_HIGH
    if (
        action_kind in _ROLE_ACTIONS
        and role_permissions is not None
        and role_is_dangerous(role_permissions)
    ):
        return RISK_HIGH
    return RISK_LOW


def requires_confirmation(action_kind: str, *, role_permissions: int | None = None) -> bool:
    """High-risk actions require an explicit confirmation token before execution."""
    return classify_risk(action_kind, role_permissions=role_permissions) == RISK_HIGH


def audit_reason(actor_name: str, actor_id: int, action: str) -> str:
    """Assemble an X-Audit-Log-Reason that attributes the change to the human caller (clamped)."""
    reason = f"dcm admin: {action} | by {actor_name} ({actor_id})"
    return reason[:AUDIT_REASON_MAX]


def make_confirmation_token() -> str:
    return secrets.token_hex(8)


@dataclass
class _Pending:
    token: str
    summary: str
    created_at: float


@dataclass
class PendingConfirmations:
    """Adapter-local, plain-data carry of pending high-risk ops (ralplan S2 boundary contract).

    No discord types cross this boundary. A high-risk op is registered and returns a token;
    dispatch must refuse to execute a high-risk op unless a matching, unexpired token is
    consumed. This keeps the stop-first guard (Principle 4) structural rather than by-memory.
    """

    ttl_seconds: float = 300.0
    _items: dict[str, _Pending] = field(default_factory=dict)

    def register(self, summary: str, *, now: float | None = None) -> str:
        now = time.time() if now is None else now
        # Lazy sweep: drop expired-but-never-consumed tokens so _items can't grow unbounded (S6).
        self._items = {
            t: p for t, p in self._items.items() if (now - p.created_at) <= self.ttl_seconds
        }
        token = make_confirmation_token()
        self._items[token] = _Pending(token, summary, now)
        return token

    def consume(self, token: str, *, now: float | None = None) -> bool:
        """Consume a token once. Returns True only for a known, unexpired token."""
        now = time.time() if now is None else now
        pending = self._items.pop(token, None)
        if pending is None:
            return False
        return (now - pending.created_at) <= self.ttl_seconds

    def pending_summary(self, token: str) -> str | None:
        item = self._items.get(token)
        return item.summary if item else None


@dataclass
class OpResult:
    """Outcome of a policy-applied guild-management op (plain-data, discord-free)."""

    ok: bool
    detail: str
    needs_confirmation: bool = False
    confirmation_token: str | None = None
    risk: str = RISK_LOW


class GuildAdminService:
    """Applies guild-management policy on top of a ``GuildAdmin`` (DIP) — never imports discord.

    Decides risk + whether confirmation is required + the X-Audit-Log-Reason, then calls the
    GuildAdmin primitive. High-risk ops return ``needs_confirmation`` with a token instead of
    executing; the adapter renders confirmation and re-calls with ``confirm_token``; the policy
    layer consumes it (single-use + expiry) (ralplan S2 boundary contract / S6 guard enforcement).
    """

    def __init__(self, admin: GuildAdmin, pending: PendingConfirmations) -> None:
        self._admin = admin
        self._pending = pending
        # confirm_token → 파싱된 ServerTemplate (드라이런에서 캐시 → confirm 시 실행). 단일 사용(pop).
        self._template_cache: dict = {}

    def _gate(self, action: str, summary: str, *, role_permissions: int | None, confirm_token: str | None):
        """Confirmation gate for high-risk ops (ralplan S2/S6). Issue a single-use token when
        unconfirmed; CONSUME it in the policy layer when supplied — so single-use/expiry is
        enforced where the token is issued, not by adapter trust. Returns an OpResult to short-
        circuit (confirmation needed / bad token) or None to proceed."""
        if not requires_confirmation(action, role_permissions=role_permissions):
            return None
        if confirm_token is None:
            token = self._pending.register(summary)
            return OpResult(
                ok=False,
                detail=f"확인 필요(고위험): {summary}",
                needs_confirmation=True,
                confirmation_token=token,
                risk=RISK_HIGH,
            )
        if not self._pending.consume(confirm_token):
            return OpResult(
                ok=False,
                detail=f"확인 토큰이 유효하지 않거나 만료됨: {summary}",
                needs_confirmation=True,
                risk=RISK_HIGH,
            )
        return None

    async def create_category(self, *, guild_id, actor_name, actor_id, name) -> OpResult:
        reason = audit_reason(actor_name, actor_id, f"create category '{name}'")
        cid = await self._admin.create_category(guild_id, name, reason=reason)
        return OpResult(True, f"카테고리 생성됨: {name} ({cid})")

    async def create_channel(
        self, *, guild_id, actor_name, actor_id, name, kind, category_id=None
    ) -> OpResult:
        reason = audit_reason(actor_name, actor_id, f"create {kind} channel '{name}'")
        cid = await self._admin.create_channel(guild_id, name, kind, category_id, reason=reason)
        return OpResult(True, f"{kind} 채널 생성됨: {name} ({cid})")

    async def edit_channel(
        self, *, guild_id, actor_name, actor_id, channel_id, name=None, category_id=None
    ) -> OpResult:
        reason = audit_reason(actor_name, actor_id, f"edit channel {channel_id}")
        await self._admin.edit_channel(
            guild_id, channel_id, name=name, category_id=category_id, reason=reason
        )
        return OpResult(True, f"채널 수정됨: {channel_id}")

    async def delete_channel(
        self, *, guild_id, actor_name, actor_id, channel_id, confirm_token=None
    ) -> OpResult:
        gate = self._gate(
            "delete_channel", f"채널 삭제 {channel_id}", role_permissions=None, confirm_token=confirm_token
        )
        if gate is not None:
            return gate
        reason = audit_reason(actor_name, actor_id, f"delete channel {channel_id}")
        await self._admin.delete_channel(guild_id, channel_id, reason=reason)
        return OpResult(True, f"채널 삭제됨: {channel_id}", risk=RISK_HIGH)

    async def create_role(
        self, *, guild_id, actor_name, actor_id, name, permissions=0, confirm_token=None
    ) -> OpResult:
        gate = self._gate(
            "create_role",
            f"역할 생성 '{name}' (관리권한 포함)",
            role_permissions=permissions,
            confirm_token=confirm_token,
        )
        if gate is not None:
            return gate
        reason = audit_reason(actor_name, actor_id, f"create role '{name}'")
        rid = await self._admin.create_role(guild_id, name, permissions=permissions, reason=reason)
        risk = classify_risk("create_role", role_permissions=permissions)
        return OpResult(True, f"역할 생성됨: {name} ({rid})", risk=risk)

    async def assign_role(
        self, *, guild_id, actor_name, actor_id, user_id, role_id, confirm_token=None
    ) -> OpResult:
        perms = await self._admin.role_permissions(guild_id, role_id)
        gate = self._gate(
            "assign_role",
            f"역할 부여 {role_id} → 멤버 {user_id} (관리권한 역할)",
            role_permissions=perms,
            confirm_token=confirm_token,
        )
        if gate is not None:
            return gate
        reason = audit_reason(actor_name, actor_id, f"assign role {role_id} to {user_id}")
        await self._admin.assign_role(guild_id, user_id, role_id, reason=reason)
        return OpResult(True, f"역할 부여됨: {role_id} → {user_id}", risk=classify_risk("assign_role", role_permissions=perms))

    async def remove_role(
        self, *, guild_id, actor_name, actor_id, user_id, role_id
    ) -> OpResult:
        reason = audit_reason(actor_name, actor_id, f"remove role {role_id} from {user_id}")
        await self._admin.remove_role(guild_id, user_id, role_id, reason=reason)
        return OpResult(True, f"역할 회수됨: {role_id} → {user_id}")

    async def set_role_permissions(
        self, *, guild_id, actor_name, actor_id, role_id, permissions, confirm_token=None
    ) -> OpResult:
        gate = self._gate(
            "set_role_permissions",
            f"역할 권한 설정 {role_id} (관리권한 포함)",
            role_permissions=permissions,
            confirm_token=confirm_token,
        )
        if gate is not None:
            return gate
        reason = audit_reason(actor_name, actor_id, f"set permissions on role {role_id}")
        await self._admin.set_role_permissions(guild_id, role_id, permissions, reason=reason)
        return OpResult(True, f"역할 권한 설정됨: {role_id}", risk=classify_risk("set_role_permissions", role_permissions=permissions))

    async def create_project(
        self, *, guild_id, actor_name, actor_id, name, channels, access_role_name, confirm_token=None
    ) -> OpResult:
        """Create a 'project set' in one command: a dedicated access role + a category + text
        channels private to that role (deny @everyone view, allow the role). Always high-risk
        (bundled + creates a role). Partial failure reports what was created; no auto-rollback
        (Discord has no transactions) — ralplan S5."""
        if not channels:
            return OpResult(False, "프로젝트 세트에는 채널이 최소 1개 필요해.", risk=RISK_HIGH)
        gate = self._gate(
            "create_project", f"프로젝트 세트 '{name}' 생성", role_permissions=None, confirm_token=confirm_token
        )
        if gate is not None:
            return gate
        reason = audit_reason(actor_name, actor_id, f"create project '{name}'")
        created: list[str] = []
        try:
            role_id = await self._admin.create_role(guild_id, access_role_name, permissions=0, reason=reason)
            created.append(f"role={role_id}")
            category_id = await self._admin.create_category(guild_id, name, reason=reason)
            created.append(f"category={category_id}")
            for ch in channels:
                cid = await self._admin.create_channel(
                    guild_id, ch, "text", int(category_id), reason=reason
                )
                created.append(f"channel={cid}")
                # private: deny @everyone (role id == guild id), allow the access role
                await self._admin.set_channel_role_overwrite(
                    guild_id, int(cid), guild_id, view=False, reason=reason
                )
                await self._admin.set_channel_role_overwrite(
                    guild_id, int(cid), int(role_id), view=True, reason=reason
                )
        except Exception as exc:  # noqa: BLE001 - partial-failure report, no auto-rollback (S5)
            return OpResult(
                False,
                f"프로젝트 세트 '{name}' 부분 실패 — 생성됨: {', '.join(created) or '없음'}; 중단: {exc}. 자동 롤백 없음(생성분은 수동 정리 필요).",
                risk=RISK_HIGH,
            )
        return OpResult(
            True,
            f"프로젝트 세트 '{name}' 생성 완료 — 접근역할 + 카테고리 + 채널 {len(channels)}개. {', '.join(created)}",
            risk=RISK_HIGH,
        )
    async def kick_member(
        self, *, guild_id, actor_name, actor_id, user_id, confirm_token=None
    ) -> OpResult:
        """멤버 추방 (고위험, confirm 필요)."""
        summary = f"멤버 추방: {user_id}"
        gate = self._gate("kick_member", summary, role_permissions=None, confirm_token=confirm_token)
        if gate is not None:
            return gate
        reason = audit_reason(actor_name, actor_id, f"kick member {user_id}")
        await self._admin.kick_member(guild_id, int(user_id), reason=reason)
        return OpResult(True, f"멤버 추방됨: {user_id}", risk=RISK_HIGH)

    async def ban_member(
        self, *, guild_id, actor_name, actor_id, user_id, confirm_token=None
    ) -> OpResult:
        """멤버 차단(밴) (고위험, confirm 필요)."""
        summary = f"멤버 차단(밴): {user_id}"
        gate = self._gate("ban_member", summary, role_permissions=None, confirm_token=confirm_token)
        if gate is not None:
            return gate
        reason = audit_reason(actor_name, actor_id, f"ban member {user_id}")
        await self._admin.ban_member(guild_id, int(user_id), reason=reason)
        return OpResult(True, f"멤버 차단됨: {user_id}", risk=RISK_HIGH)

    async def timeout_member(
        self, *, guild_id, actor_name, actor_id, user_id, duration_seconds
    ) -> OpResult:
        """멤버 타임아웃 (저위험, 즉시 실행)."""
        reason = audit_reason(
            actor_name, actor_id, f"timeout member {user_id} for {duration_seconds}s"
        )
        await self._admin.timeout_member(guild_id, int(user_id), int(duration_seconds), reason=reason)
        return OpResult(True, f"멤버 타임아웃 적용됨: {user_id} ({duration_seconds}초)", risk=RISK_LOW)

    async def purge_messages(
        self, *, guild_id, actor_name, actor_id, channel_id, count, confirm_token=None
    ) -> OpResult:
        """채널 메시지 대량 삭제 (count > 100 하드 거부; 고위험, confirm 필요)."""
        count = int(count)
        if count > 100:
            return OpResult(
                False,
                f"메시지 삭제 상한 초과: Discord 벌크 삭제는 한 번에 최대 100건까지 가능해 (요청: {count}건).",
                needs_confirmation=False,
            )
        summary = f"채널 메시지 삭제: {channel_id} ({count}건)"
        gate = self._gate("purge_messages", summary, role_permissions=None, confirm_token=confirm_token)
        if gate is not None:
            return gate
        reason = audit_reason(actor_name, actor_id, f"purge {count} messages in channel {channel_id}")
        deleted = await self._admin.purge_messages(guild_id, int(channel_id), count, reason=reason)
        return OpResult(True, f"메시지 {deleted}건 삭제됨 (채널 {channel_id})", risk=RISK_HIGH)

    async def apply_template(
        self, *, guild_id, actor_name, actor_id, template_text=None, confirm_token=None
    ) -> OpResult:
        """서버 블루프린트 적용 (고위험, 항상 confirm). confirm_token이 없으면 파싱+검증 후
        미리보기와 단일 토큰을 돌려주고 계획을 캐시한다(드라이런). confirm_token이 주어지면
        캐시된 계획을 멱등 실행한다 — 같은 이름의 역할/카테고리/채널은 건너뛰고 재사용."""
        from .template import TemplateError, parse_template  # lazy: import cycle 회피

        if confirm_token is not None:
            plan = self._template_cache.pop(confirm_token, None)
            if plan is None:
                return OpResult(
                    False,
                    "템플릿 확인 토큰이 유효하지 않거나 만료됐어. 파일을 다시 올려줘.",
                    needs_confirmation=True,
                    risk=RISK_HIGH,
                )
            return await self._execute_template(guild_id, actor_name, actor_id, plan)

        if not template_text:
            return OpResult(False, "템플릿 파일이 필요해 (.yaml/.yml/.json).", needs_confirmation=False)
        try:
            plan = parse_template(template_text)
        except TemplateError as exc:
            return OpResult(False, f"템플릿 오류: {exc}", needs_confirmation=False)
        token = make_confirmation_token()
        self._template_cache[token] = plan
        return OpResult(
            False,
            plan.summary(),
            needs_confirmation=True,
            confirmation_token=token,
            risk=RISK_HIGH,
        )

    async def _execute_template(self, guild_id, actor_name, actor_id, plan) -> OpResult:
        reason = audit_reason(actor_name, actor_id, "apply server template")
        created: list[str] = []
        try:
            existing_roles = {
                r["name"]: int(r["id"]) for r in await self._admin.list_roles(guild_id)
            }
            existing_channels = await self._admin.list_channels(guild_id)
            role_ids = dict(existing_roles)  # name -> id (기존 + 신규)

            for r in plan.roles:
                if r.name in role_ids:
                    continue
                rid = await self._admin.create_role(
                    guild_id, r.name, permissions=r.permission_bits, reason=reason
                )
                role_ids[r.name] = int(rid)
                created.append(f"역할 {r.name}")

            cats_by_name = {
                c["name"]: int(c["id"]) for c in existing_channels if c.get("type") == 4
            }
            for cat in plan.categories:
                cat_id = cats_by_name.get(cat.name)
                if cat_id is None:
                    cat_id = int(
                        await self._admin.create_category(guild_id, cat.name, reason=reason)
                    )
                    cats_by_name[cat.name] = cat_id
                    created.append(f"카테고리 {cat.name}")
                if cat.private:
                    # @everyone(역할 id == 길드 id) 열람 차단 → 지정 역할만 허용 (자식 채널 상속)
                    await self._admin.set_channel_role_overwrite(
                        guild_id, cat_id, guild_id, view=False, reason=reason
                    )
                    for rolename in cat.visible_to:
                        vid = role_ids.get(rolename)
                        if vid is not None:
                            await self._admin.set_channel_role_overwrite(
                                guild_id, cat_id, vid, view=True, reason=reason
                            )
                existing_under = {
                    (c["name"].lower(), int(c["type"]))
                    for c in existing_channels
                    if c.get("parent_id") and int(c["parent_id"]) == cat_id
                }
                for ch in cat.channels:
                    disc_type = 2 if ch.kind == "voice" else 0
                    if (ch.name.lower(), disc_type) in existing_under:
                        continue
                    await self._admin.create_channel(
                        guild_id, ch.name, ch.kind, cat_id, reason=reason
                    )
                    created.append(f"{ch.kind} 채널 {ch.name}")
        except Exception as exc:  # noqa: BLE001 - 부분 실패 보고, 자동 롤백 없음(트랜잭션 불가)
            return OpResult(
                False,
                f"템플릿 적용 부분 실패 — 생성됨: {', '.join(created) or '없음'}; 중단: {exc}. 자동 롤백 없음.",
                risk=RISK_HIGH,
            )
        if not created:
            return OpResult(True, "템플릿 적용 완료 — 변경 없음(모두 이미 존재).", risk=RISK_HIGH)
        return OpResult(True, ("템플릿 적용 완료 ✅ — " + ", ".join(created))[:1900], risk=RISK_HIGH)


class RateLimiter:
    """Additive burst-smoothing limiter layered ON TOP of pycord's built-in 429 handling
    (ralplan S6; non-gated). Serializes calls and enforces a minimum interval to avoid 403 /
    invalid-request storms during bulk operations (e.g. /create-project). Pure asyncio, no discord.
    """

    def __init__(self, min_interval: float = 0.05) -> None:
        self._min = min_interval
        self._lock = asyncio.Lock()
        self._last = float("-inf")  # first acquire never waits

    async def acquire(self, *, now=None, sleep=None) -> float:
        """Block until at least ``min_interval`` has elapsed since the previous call. Returns the
        wait actually applied (0.0 if none). ``now``/``sleep`` are injectable for tests."""
        import time as _time

        now = now or _time.monotonic
        sleep = sleep or asyncio.sleep
        async with self._lock:
            t = now()
            wait = self._min - (t - self._last)
            if wait > 0:
                await sleep(wait)
                self._last = t + wait
                return wait
            self._last = t
            return 0.0
