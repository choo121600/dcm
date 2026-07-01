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

from ..i18n import t
from .cleanup import (
    ARCHIVE_CATEGORY_BASE,
    DEFAULT_INACTIVE_DAYS,
    MAX_CHANNELS_PER_CATEGORY,
    find_archive_category_ids,
    plan_cleanup,
)

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
        # confirm_token → parsed ServerTemplate (cached during dry-run → executed on confirm). Single-use (pop).
        self._template_cache: dict = {}
        # confirm_token → CleanupPlan (cached during dry-run → executed on confirm). Single-use (pop).
        self._cleanup_cache: dict = {}

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
                detail=t("guild_admin.confirm_needed", summary=summary),
                needs_confirmation=True,
                confirmation_token=token,
                risk=RISK_HIGH,
            )
        if not self._pending.consume(confirm_token):
            return OpResult(
                ok=False,
                detail=t("guild_admin.confirm_token_invalid", summary=summary),
                needs_confirmation=True,
                risk=RISK_HIGH,
            )
        return None

    async def create_category(self, *, guild_id, actor_name, actor_id, name) -> OpResult:
        reason = audit_reason(actor_name, actor_id, f"create category '{name}'")
        cid = await self._admin.create_category(guild_id, name, reason=reason)
        return OpResult(True, t("guild_admin.category_created", name=name, cid=cid))

    async def create_channel(
        self, *, guild_id, actor_name, actor_id, name, kind, category_id=None
    ) -> OpResult:
        reason = audit_reason(actor_name, actor_id, f"create {kind} channel '{name}'")
        cid = await self._admin.create_channel(guild_id, name, kind, category_id, reason=reason)
        return OpResult(True, t("guild_admin.channel_created", kind=kind, name=name, cid=cid))

    async def edit_channel(
        self, *, guild_id, actor_name, actor_id, channel_id, name=None, category_id=None
    ) -> OpResult:
        reason = audit_reason(actor_name, actor_id, f"edit channel {channel_id}")
        await self._admin.edit_channel(
            guild_id, channel_id, name=name, category_id=category_id, reason=reason
        )
        return OpResult(True, t("guild_admin.channel_edited", channel_id=channel_id))

    async def delete_channel(
        self, *, guild_id, actor_name, actor_id, channel_id, confirm_token=None
    ) -> OpResult:
        gate = self._gate(
            "delete_channel",
            t("guild_admin.summary_delete_channel", channel_id=channel_id),
            role_permissions=None,
            confirm_token=confirm_token,
        )
        if gate is not None:
            return gate
        reason = audit_reason(actor_name, actor_id, f"delete channel {channel_id}")
        await self._admin.delete_channel(guild_id, channel_id, reason=reason)
        return OpResult(True, t("guild_admin.channel_deleted", channel_id=channel_id), risk=RISK_HIGH)

    async def create_role(
        self, *, guild_id, actor_name, actor_id, name, permissions=0, confirm_token=None
    ) -> OpResult:
        gate = self._gate(
            "create_role",
            t("guild_admin.summary_create_role", name=name),
            role_permissions=permissions,
            confirm_token=confirm_token,
        )
        if gate is not None:
            return gate
        reason = audit_reason(actor_name, actor_id, f"create role '{name}'")
        rid = await self._admin.create_role(guild_id, name, permissions=permissions, reason=reason)
        risk = classify_risk("create_role", role_permissions=permissions)
        return OpResult(True, t("guild_admin.role_created", name=name, rid=rid), risk=risk)

    async def assign_role(
        self, *, guild_id, actor_name, actor_id, user_id, role_id, confirm_token=None
    ) -> OpResult:
        perms = await self._admin.role_permissions(guild_id, role_id)
        gate = self._gate(
            "assign_role",
            t("guild_admin.summary_assign_role", role_id=role_id, user_id=user_id),
            role_permissions=perms,
            confirm_token=confirm_token,
        )
        if gate is not None:
            return gate
        reason = audit_reason(actor_name, actor_id, f"assign role {role_id} to {user_id}")
        await self._admin.assign_role(guild_id, user_id, role_id, reason=reason)
        return OpResult(True, t("guild_admin.role_assigned", role_id=role_id, user_id=user_id), risk=classify_risk("assign_role", role_permissions=perms))

    async def remove_role(
        self, *, guild_id, actor_name, actor_id, user_id, role_id
    ) -> OpResult:
        reason = audit_reason(actor_name, actor_id, f"remove role {role_id} from {user_id}")
        await self._admin.remove_role(guild_id, user_id, role_id, reason=reason)
        return OpResult(True, t("guild_admin.role_removed", role_id=role_id, user_id=user_id))

    async def set_role_permissions(
        self, *, guild_id, actor_name, actor_id, role_id, permissions, confirm_token=None
    ) -> OpResult:
        gate = self._gate(
            "set_role_permissions",
            t("guild_admin.summary_set_role_permissions", role_id=role_id),
            role_permissions=permissions,
            confirm_token=confirm_token,
        )
        if gate is not None:
            return gate
        reason = audit_reason(actor_name, actor_id, f"set permissions on role {role_id}")
        await self._admin.set_role_permissions(guild_id, role_id, permissions, reason=reason)
        return OpResult(True, t("guild_admin.role_permissions_set", role_id=role_id), risk=classify_risk("set_role_permissions", role_permissions=permissions))

    async def create_project(
        self, *, guild_id, actor_name, actor_id, name, channels, access_role_name, confirm_token=None
    ) -> OpResult:
        """Create a 'project set' in one command: a dedicated access role + a category + text
        channels private to that role (deny @everyone view, allow the role). Always high-risk
        (bundled + creates a role). Partial failure reports what was created; no auto-rollback
        (Discord has no transactions) — ralplan S5."""
        if not channels:
            return OpResult(False, t("guild_admin.project_needs_channel"), risk=RISK_HIGH)
        gate = self._gate(
            "create_project",
            t("guild_admin.summary_create_project", name=name),
            role_permissions=None,
            confirm_token=confirm_token,
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
            created_str = ", ".join(created) or t("guild_admin.none_label")
            return OpResult(
                False,
                t("guild_admin.project_partial_fail", name=name, created=created_str, exc=exc),
                risk=RISK_HIGH,
            )
        return OpResult(
            True,
            t("guild_admin.project_created", name=name, count=len(channels), created=", ".join(created)),
            risk=RISK_HIGH,
        )
    async def kick_member(
        self, *, guild_id, actor_name, actor_id, user_id, confirm_token=None
    ) -> OpResult:
        """Kick a member (high-risk, requires confirmation)."""
        summary = t("guild_admin.summary_kick", user_id=user_id)
        gate = self._gate("kick_member", summary, role_permissions=None, confirm_token=confirm_token)
        if gate is not None:
            return gate
        reason = audit_reason(actor_name, actor_id, f"kick member {user_id}")
        await self._admin.kick_member(guild_id, int(user_id), reason=reason)
        return OpResult(True, t("guild_admin.member_kicked", user_id=user_id), risk=RISK_HIGH)

    async def ban_member(
        self, *, guild_id, actor_name, actor_id, user_id, confirm_token=None
    ) -> OpResult:
        """Ban a member (high-risk, requires confirmation)."""
        summary = t("guild_admin.summary_ban", user_id=user_id)
        gate = self._gate("ban_member", summary, role_permissions=None, confirm_token=confirm_token)
        if gate is not None:
            return gate
        reason = audit_reason(actor_name, actor_id, f"ban member {user_id}")
        await self._admin.ban_member(guild_id, int(user_id), reason=reason)
        return OpResult(True, t("guild_admin.member_banned", user_id=user_id), risk=RISK_HIGH)

    async def timeout_member(
        self, *, guild_id, actor_name, actor_id, user_id, duration_seconds
    ) -> OpResult:
        """Time out a member (low-risk, executes immediately)."""
        reason = audit_reason(
            actor_name, actor_id, f"timeout member {user_id} for {duration_seconds}s"
        )
        await self._admin.timeout_member(guild_id, int(user_id), int(duration_seconds), reason=reason)
        return OpResult(True, t("guild_admin.member_timed_out", user_id=user_id, duration=duration_seconds), risk=RISK_LOW)

    async def purge_messages(
        self, *, guild_id, actor_name, actor_id, channel_id, count, confirm_token=None
    ) -> OpResult:
        """Bulk-delete channel messages (count > 100 is hard-rejected; high-risk, requires confirmation)."""
        count = int(count)
        if count > 100:
            return OpResult(
                False,
                t("guild_admin.purge_over_limit", count=count),
                needs_confirmation=False,
            )
        summary = t("guild_admin.summary_purge", channel_id=channel_id, count=count)
        gate = self._gate("purge_messages", summary, role_permissions=None, confirm_token=confirm_token)
        if gate is not None:
            return gate
        reason = audit_reason(actor_name, actor_id, f"purge {count} messages in channel {channel_id}")
        deleted = await self._admin.purge_messages(guild_id, int(channel_id), count, reason=reason)
        return OpResult(True, t("guild_admin.messages_purged", deleted=deleted, channel_id=channel_id), risk=RISK_HIGH)

    async def apply_template(
        self, *, guild_id, actor_name, actor_id, template_text=None, confirm_token=None
    ) -> OpResult:
        """Apply a server blueprint (high-risk, always confirmed). Without a confirm_token, parse +
        validate, then return a preview and a single token and cache the plan (dry-run). When a
        confirm_token is given, execute the cached plan idempotently — roles/categories/channels
        with the same name are skipped and reused."""
        from .template import TemplateError, parse_template  # lazy: avoid import cycle

        if confirm_token is not None:
            plan = self._template_cache.pop(confirm_token, None)
            if plan is None:
                return OpResult(
                    False,
                    t("guild_admin.template_token_invalid"),
                    needs_confirmation=True,
                    risk=RISK_HIGH,
                )
            return await self._execute_template(guild_id, actor_name, actor_id, plan)

        if not template_text:
            return OpResult(False, t("guild_admin.template_file_needed"), needs_confirmation=False)
        try:
            plan = parse_template(template_text)
        except TemplateError as exc:
            return OpResult(False, t("guild_admin.template_error", exc=exc), needs_confirmation=False)
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
            role_ids = dict(existing_roles)  # name -> id (existing + newly created)

            for r in plan.roles:
                if r.name in role_ids:
                    continue
                rid = await self._admin.create_role(
                    guild_id, r.name, permissions=r.permission_bits, reason=reason
                )
                role_ids[r.name] = int(rid)
                created.append(t("guild_admin.created_role", name=r.name))

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
                    created.append(t("guild_admin.created_category", name=cat.name))
                if cat.private:
                    # block @everyone (role id == guild id) from viewing → allow only the designated roles (children inherit)
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
                    created.append(t("guild_admin.created_channel", kind=ch.kind, name=ch.name))
        except Exception as exc:  # noqa: BLE001 - report partial failure, no auto-rollback (no transactions)
            created_str = ", ".join(created) or t("guild_admin.none_label")
            return OpResult(
                False,
                t("guild_admin.template_partial_fail", created=created_str, exc=exc),
                risk=RISK_HIGH,
            )
        if not created:
            return OpResult(True, t("guild_admin.template_no_change"), risk=RISK_HIGH)
        return OpResult(True, t("guild_admin.template_applied", created=", ".join(created))[:1900], risk=RISK_HIGH)

    async def cleanup_report(
        self, *, guild_id, inactive_days=DEFAULT_INACTIVE_DAYS, admin_role_id=0, welcome_channel_id=0, protected_role_ids=()
    ) -> OpResult:
        """Read-only: compute inactive-channel + orphan-role candidates and return only a summary (no changes)."""
        channels = await self._admin.list_channels(guild_id)
        roles = await self._admin.list_roles(guild_id)
        plan = plan_cleanup(
            channels,
            roles,
            now_ms=time.time() * 1000,
            inactive_days=inactive_days,
            admin_role_id=admin_role_id,
            welcome_channel_id=welcome_channel_id,
            protected_role_ids=protected_role_ids,
        )
        return OpResult(True, plan.summary()[:1900])

    async def cleanup_archive(
        self,
        *,
        guild_id,
        actor_name,
        actor_id,
        inactive_days=DEFAULT_INACTIVE_DAYS,
        admin_role_id=0,
        welcome_channel_id=0,
        protected_role_ids=(),
        confirm_token=None,
    ) -> OpResult:
        """Move inactive channels into the '📦 아카이브' category (+ hide from members). Reversible. Dry-run→token→execute."""
        channels = await self._admin.list_channels(guild_id)
        plan = plan_cleanup(
            channels,
            [],
            now_ms=time.time() * 1000,
            inactive_days=inactive_days,
            admin_role_id=admin_role_id,
            welcome_channel_id=welcome_channel_id,
            protected_role_ids=protected_role_ids,
        )
        if confirm_token is not None:
            if self._cleanup_cache.pop(confirm_token, None) != "archive":
                return OpResult(
                    False,
                    t("guild_admin.archive_token_invalid"),
                    needs_confirmation=True,
                    risk=RISK_HIGH,
                )
            return await self._execute_archive(guild_id, actor_name, actor_id, channels, plan)
        if not plan.archive_channels:
            return OpResult(True, t("guild_admin.archive_none"))
        token = make_confirmation_token()
        self._cleanup_cache[token] = "archive"
        return OpResult(
            False,
            plan.archive_summary()[:1900],
            needs_confirmation=True,
            confirmation_token=token,
            risk=RISK_HIGH,
        )

    async def cleanup_purge(
        self,
        *,
        guild_id,
        actor_name,
        actor_id,
        inactive_days=DEFAULT_INACTIVE_DAYS,
        admin_role_id=0,
        welcome_channel_id=0,
        protected_role_ids=(),
        confirm_token=None,
    ) -> OpResult:
        """Delete all channels inside '📦 아카이브' + delete orphan roles (permanent, always confirmed). Dry-run→token→execute."""
        channels = await self._admin.list_channels(guild_id)
        roles = await self._admin.list_roles(guild_id)
        plan = plan_cleanup(
            channels,
            roles,
            now_ms=time.time() * 1000,
            inactive_days=inactive_days,
            admin_role_id=admin_role_id,
            welcome_channel_id=welcome_channel_id,
            protected_role_ids=protected_role_ids,
        )
        if confirm_token is not None:
            if self._cleanup_cache.pop(confirm_token, None) != "purge":
                return OpResult(
                    False,
                    t("guild_admin.purge_token_invalid"),
                    needs_confirmation=True,
                    risk=RISK_HIGH,
                )
            return await self._execute_purge(guild_id, actor_name, actor_id, channels, plan)
        if not plan.purge_channels and not plan.delete_roles and not plan.orphan_categories:
            return OpResult(True, t("guild_admin.purge_none"))
        token = make_confirmation_token()
        self._cleanup_cache[token] = "purge"
        return OpResult(
            False,
            plan.purge_summary()[:1900],
            needs_confirmation=True,
            confirmation_token=token,
            risk=RISK_HIGH,
        )

    async def _execute_archive(self, guild_id, actor_name, actor_id, channels, plan) -> OpResult:
        reason = audit_reason(actor_name, actor_id, "archive inactive channels")
        counts: dict[int, int] = {}
        for c in channels:
            p = c.get("parent_id")
            if p:
                counts[int(p)] = counts.get(int(p), 0) + 1
        slots = [
            [cid, MAX_CHANNELS_PER_CATEGORY - counts.get(cid, 0)]
            for cid in sorted(find_archive_category_ids(channels))
        ]
        moved: list[str] = []
        failed: list[str] = []

        async def slot_with_space():
            for s in slots:
                if s[1] > 0:
                    return s
            n = len(slots) + 1
            nm = ARCHIVE_CATEGORY_BASE if n == 1 else f"{ARCHIVE_CATEGORY_BASE} {n}"
            nid = int(await self._admin.create_category(guild_id, nm, reason=reason))
            await self._admin.set_channel_role_overwrite(guild_id, nid, guild_id, view=False, reason=reason)
            s = [nid, MAX_CHANNELS_PER_CATEGORY]
            slots.append(s)
            return s

        for ch in plan.archive_channels:
            try:
                s = await slot_with_space()
                await self._admin.edit_channel(guild_id, ch.id, category_id=s[0], reason=reason)
                await self._admin.set_channel_role_overwrite(guild_id, ch.id, guild_id, view=False, reason=reason)
                s[1] -= 1
                moved.append(ch.name)
            except Exception as exc:  # noqa: BLE001 - report partial failure, no auto-rollback
                failed.append(t("guild_admin.fail_channel", name=ch.name, exc=exc))
        parts = [t("guild_admin.archive_moved", count=len(moved))]
        if failed:
            parts.append(t("guild_admin.fail_count", count=len(failed), details="; ".join(failed[:5])))
        return OpResult(True, t("guild_admin.archive_done", parts=", ".join(parts))[:1900], risk=RISK_HIGH)

    async def _execute_purge(self, guild_id, actor_name, actor_id, channels, plan) -> OpResult:
        reason = audit_reason(actor_name, actor_id, "purge archive channels + orphan roles")
        deleted_ch: list[str] = []
        deleted_role: list[str] = []
        failed: list[str] = []
        for ch in plan.purge_channels:
            try:
                await self._admin.delete_channel(guild_id, ch.id, reason=reason)
                deleted_ch.append(ch.name)
            except Exception as exc:  # noqa: BLE001
                failed.append(t("guild_admin.fail_channel", name=ch.name, exc=exc))
        for r in plan.delete_roles:
            try:
                await self._admin.delete_role(guild_id, r.id, reason=reason)
                deleted_role.append(r.name)
            except Exception as exc:  # noqa: BLE001
                failed.append(t("guild_admin.fail_role", name=r.name, exc=exc))
        deleted_cat = 0
        # clean up emptied archive categories + orphan (empty) categories
        for cid in find_archive_category_ids(channels):
            try:
                await self._admin.delete_channel(guild_id, cid, reason=reason)
                deleted_cat += 1
            except Exception:  # noqa: BLE001
                pass
        for cat in plan.orphan_categories:
            try:
                await self._admin.delete_channel(guild_id, cat.id, reason=reason)
                deleted_cat += 1
            except Exception as exc:  # noqa: BLE001
                failed.append(t("guild_admin.fail_category", name=cat.name, exc=exc))
        parts = [
            t("guild_admin.purged_channels", count=len(deleted_ch)),
            t("guild_admin.purged_roles", count=len(deleted_role)),
            t("guild_admin.purged_categories", count=deleted_cat),
        ]
        if failed:
            parts.append(t("guild_admin.fail_count", count=len(failed), details="; ".join(failed[:5])))
        return OpResult(True, t("guild_admin.purge_done", parts=", ".join(parts))[:1900], risk=RISK_HIGH)


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
