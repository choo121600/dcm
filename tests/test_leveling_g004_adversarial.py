"""G004 adversarial / security regression tests.

Complements tests/test_leveling_g004.py — zero duplication of existing test IDs.
Every case probes a security-critical path:

  * unattended role auto-grant allow-list (reconcile_roles / _role_grant_ok)
  * Orchestrator LLM quota gate (boundary, falsy guild_id)
  * Admin leveling slash-command InvokerCheck (set/remove/list-level-role)

Run:
  uv run pytest tests/test_leveling_g004_adversarial.py tests/test_leveling_g004.py tests/test_authz.py -q
"""
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

from dcm.leveling.scoring import cum_cost, utc_day
from dcm.leveling.service import DANGEROUS_PERMISSION_NAMES, LevelingService
from dcm.leveling.store import LevelingStore
from dcm.orchestrator import Orchestrator
from dcm.platform.base import IncomingMessage
from dcm.platform.pycord_adapter import PycordAdapter

# ──────────────────────────────── helpers ────────────────────────────────────

_ADMIN_ROLE_ID = 999


def _service(tmp: str):
    store = LevelingStore(os.path.join(tmp, "leveling.db"))
    return LevelingService(store), store


def _persona(tmp: str) -> str:
    path = os.path.join(tmp, "persona.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("너는 지우.")
    return path


def _incoming(guild_id=1, author_id: str = "42", text: str = "안녕 지우야"):
    return IncomingMessage(
        channel_id="c",
        author_id=author_id,
        author_name="춘식",
        content=text,
        guild_id=guild_id,
    )


def _orch(tmp, leveling, *, router=None):
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=("페르소나 응답", False))
    orch = Orchestrator(
        llm=llm,
        persona_path=Path(_persona(tmp)),
        bot_name="지우",
        max_input_chars=4000,
        leveling=leveling,
        router=router,
    )
    return orch, llm


def _perms(**on):
    """Build a permissions namespace with all DANGEROUS_PERMISSION_NAMES defaulting to False."""
    p = types.SimpleNamespace()
    for name in DANGEROUS_PERMISSION_NAMES:
        setattr(p, name, False)
    for name, val in on.items():
        setattr(p, name, val)
    return p


def _role(role_id=100, position=1, managed=False, *, has_perms=True, **dangerous):
    """Build a fake discord role.  Pass has_perms=False to omit the permissions attribute."""
    r = types.SimpleNamespace(id=role_id, position=position, managed=managed, name=f"role{role_id}")
    if has_perms:
        r.permissions = _perms(**dangerous)
    return r


def _guild_ns(bot_top=10, default_role=None):
    """Return a plain guild namespace (for _role_grant_ok)."""
    me = types.SimpleNamespace(top_role=types.SimpleNamespace(position=bot_top))
    return types.SimpleNamespace(me=me, default_role=default_role)


def _guild_dict(bot_top=10, *, top_role_none=False, default_role=None):
    """Return a guild dict suitable for _member().  top_role_none=True simulates bot with no top role."""
    if top_role_none:
        me = types.SimpleNamespace(top_role=None)
    else:
        me = types.SimpleNamespace(top_role=types.SimpleNamespace(position=bot_top))
    return {"id": "1", "me": me, "default_role": default_role}


def _member(guild_dict, role_lookup, *, member_id="42", roles=None):
    return types.SimpleNamespace(
        id=member_id,
        guild=types.SimpleNamespace(
            id=guild_dict["id"],
            me=guild_dict["me"],
            default_role=guild_dict.get("default_role"),
            get_role=lambda rid: role_lookup.get(int(rid)),
        ),
        roles=list(roles) if roles is not None else [],
        add_roles=AsyncMock(),
    )


# ─────────────────── _role_grant_ok adversarial ──────────────────────────────


def test_multiple_dangerous_bits_simultaneously_rejected():
    """A role carrying several dangerous permissions at once must be rejected.

    Security contract: the allow-list guard must not pass a role that combines e.g.
    administrator + manage_roles + ban_members, even if the check short-circuits on the
    first dangerous bit found.
    """
    evil_role = types.SimpleNamespace(
        id=777,
        position=1,
        managed=False,
        name="super-evil",
        permissions=_perms(administrator=True, manage_roles=True, ban_members=True),
    )
    ok, reason = LevelingService._role_grant_ok(evil_role, _guild_ns(bot_top=10), 10)
    assert ok is False, "Multi-dangerous-bit role must be rejected"
    # Reason must name one of the flagged permissions
    assert any(p in reason for p in ("administrator", "manage_roles", "ban_members")), (
        f"Unexpected rejection reason: {reason!r}"
    )


def test_role_at_exactly_bot_top_position_rejected_strict_less_than():
    """position == bot_top_position must be rejected.

    The guard is ``position >= bot_top → hierarchy``.  The equality case (position == bot_top)
    is a hierarchy violation because the bot cannot safely manage a role at its own level.
    """
    role = types.SimpleNamespace(
        id=200, position=5, managed=False, name="equal-boundary",
        permissions=_perms(),  # no dangerous bits
    )
    ok, reason = LevelingService._role_grant_ok(role, _guild_ns(bot_top=5), 5)
    assert ok is False and reason == "hierarchy", (
        f"position == bot_top must be a hierarchy rejection; got ok={ok} reason={reason!r}"
    )


def test_role_one_below_bot_top_zero_dangerous_perms_is_granted():
    """position == bot_top - 1 with no dangerous permissions must be GRANTED.

    This is the safe lower boundary: strictly less than bot_top, no privilege bits.
    """
    role = types.SimpleNamespace(
        id=201, position=4, managed=False, name="safe-boundary",
        permissions=_perms(),
    )
    ok, reason = LevelingService._role_grant_ok(role, _guild_ns(bot_top=5), 5)
    assert ok is True and reason == "ok", (
        f"position == bot_top-1 with clean perms must be granted; got ok={ok} reason={reason!r}"
    )


def test_missing_permissions_attribute_handled_gracefully():
    """A role with no ``permissions`` attribute must not raise and must be GRANTED.

    ``perms = getattr(role, 'permissions', None)`` returns None → the dangerous-perm loop
    is skipped → (True, 'ok').  No permissions == no dangerous permissions.
    """
    role = types.SimpleNamespace(id=300, position=1, managed=False, name="no-perms-attr")
    # Intentionally NO 'permissions' attribute on the namespace
    assert not hasattr(role, "permissions")
    ok, reason = LevelingService._role_grant_ok(role, _guild_ns(bot_top=10), 10)
    assert ok is True and reason == "ok", (
        f"Role with missing permissions attr should be granted; got ok={ok} reason={reason!r}"
    )


# ─────────────────── reconcile_roles adversarial ─────────────────────────────


def test_reconcile_bot_has_no_top_role_denies_all_grants():
    """When the bot member's top_role is None, bot_top resolves to None and every grant
    must be denied (no-bot-top-role guard in _role_grant_ok)."""
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.set_role_reward("1", 1, 100)
            store.add_xp("1", "42", 100_000, now=1.0)
            safe = _role(role_id=100, position=1)
            member = _member(_guild_dict(top_role_none=True), {100: safe})
            asyncio.run(svc.reconcile_roles(member))
            member.add_roles.assert_not_awaited()
        finally:
            store.close()


def test_reconcile_member_guild_none_no_crash_no_grant():
    """member.guild == None must silently degrade: no exception, no add_roles call."""
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.set_role_reward("1", 1, 100)
            store.add_xp("1", "42", 100_000, now=1.0)
            member = types.SimpleNamespace(guild=None, id="42", roles=[], add_roles=AsyncMock())
            asyncio.run(svc.reconcile_roles(member))  # must not raise
            member.add_roles.assert_not_awaited()
        finally:
            store.close()


def test_reconcile_mixed_rewards_only_safe_granted():
    """When two rewards are both reachable but one is dangerous, only the safe role is granted."""
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            # level 1 → role 100 (safe), level 2 → role 200 (dangerous: manage_roles)
            store.set_role_reward("1", 1, 100)
            store.set_role_reward("1", 2, 200)
            store.add_xp("1", "42", 100_000, now=1.0)  # level >> 2: both rewards reachable
            safe = _role(role_id=100, position=1)
            dangerous = _role(role_id=200, position=1, manage_roles=True)
            member = _member(_guild_dict(), {100: safe, 200: dangerous})
            asyncio.run(svc.reconcile_roles(member))
            member.add_roles.assert_awaited_once()
            granted_role = member.add_roles.await_args.args[0]
            assert granted_role is safe, (
                f"Expected only the safe role to be granted; granted: {granted_role}"
            )
        finally:
            store.close()


def test_reconcile_reward_requires_higher_level_than_member():
    """Reward at level N+1 must not be granted to a member exactly at level N (strict <)."""
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            xp_at_level_1 = cum_cost(1)  # exact XP to reach level 1
            store.set_role_reward("1", 2, 100)  # requires level 2
            store.add_xp("1", "42", xp_at_level_1, now=1.0)  # member is exactly level 1
            safe = _role(role_id=100, position=1)
            member = _member(_guild_dict(), {100: safe})
            asyncio.run(svc.reconcile_roles(member))
            member.add_roles.assert_not_awaited()
        finally:
            store.close()


def test_reconcile_idempotent_second_run_after_role_applied():
    """Running reconcile a second time (after Discord applies the role) must be a strict noop.

    Distinct from test_reconcile_idempotent_when_already_held: this test actually runs
    reconcile twice, simulating what happens in production when on_message fires again.
    """
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.set_role_reward("1", 1, 100)
            store.add_xp("1", "42", 100_000, now=1.0)
            safe = _role(role_id=100, position=1)

            # First run: member does not yet hold the role
            member = _member(_guild_dict(), {100: safe}, roles=[])
            asyncio.run(svc.reconcile_roles(member))
            member.add_roles.assert_awaited_once()

            # Simulate Discord having applied the role (roles list now includes safe)
            member.roles.append(safe)
            member.add_roles.reset_mock()

            # Second run: must be a noop
            asyncio.run(svc.reconcile_roles(member))
            member.add_roles.assert_not_awaited()
        finally:
            store.close()


def test_reconcile_get_role_returns_none_for_some_skips_gracefully():
    """get_role returns None for one reward (deleted role) while another is valid — must skip
    the deleted one and still grant the valid one."""
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.set_role_reward("1", 1, 999)  # deleted role (not in lookup)
            store.set_role_reward("1", 2, 100)  # valid safe role
            store.add_xp("1", "42", 100_000, now=1.0)
            safe = _role(role_id=100, position=1)
            # role 999 absent from lookup → get_role returns None
            member = _member(_guild_dict(), {100: safe})
            asyncio.run(svc.reconcile_roles(member))
            # Only the valid role should be granted
            member.add_roles.assert_awaited_once()
            assert member.add_roles.await_args.args[0] is safe
        finally:
            store.close()


# ─────────────────── LLM gate adversarial ────────────────────────────────────


def test_llm_gate_at_limit_minus_one_calls_llm_increments_to_limit():
    """used == 99 (one below the lvl-0 limit of 100) → LLM is called and usage reaches 100.

    Boundary: 99 < 100 → allowed.  After the call the counter must be 100 (quota exhausted).
    """
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            day = utc_day(time.time())
            for _ in range(99):
                store.incr_daily_usage("1", "42", day, "llm")
            orch, llm = _orch(tmp, svc)
            reply = asyncio.run(orch.handle(_incoming(), []))
            assert reply == "페르소나 응답", f"Expected normal reply; got {reply!r}"
            assert llm.complete.await_count == 1
            assert store.get_daily_usage("1", "42", day, "llm") == 100
        finally:
            store.close()


def test_llm_gate_falsy_guild_id_bypasses_gate_llm_called():
    """guild_id is falsy (None) → the LLM gate condition ``if self._leveling and incoming.guild_id``
    is False → gate skipped entirely → LLM is called normally, no crash."""
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            # Exhaust guild "1"'s quota to confirm gating is guild-scoped, not global
            day = utc_day(time.time())
            for _ in range(100):
                store.incr_daily_usage("1", "42", day, "llm")

            orch, llm = _orch(tmp, svc)
            # guild_id=None: gate must be skipped even though leveling is wired
            msg = _incoming(guild_id=None, text="안녕 지우야")
            reply = asyncio.run(orch.handle(msg, []))
            assert reply == "페르소나 응답", f"Expected LLM reply; got {reply!r}"
            assert llm.complete.await_count == 1
        finally:
            store.close()


# ─────────────────── Admin leveling command guard ────────────────────────────


class _Author:
    def __init__(self, admin: bool):
        self.roles = [types.SimpleNamespace(id=_ADMIN_ROLE_ID)] if admin else []
        self.id = 42
        self.display_name = "춘식"


class _Ctx:
    def __init__(self, admin: bool):
        self.author = _Author(admin)
        self.guild_id = 123
        self.guild = None
        self.responses: list[tuple] = []

    async def respond(self, text, **kw):
        self.responses.append((text, kw))


def _leveling_adapter_with_real_store(store_dir: str):
    """Return (adapter, leveling_service, store) with leveling commands registered.

    Only register_leveling_commands is called (not register_admin_commands) so that
    the three leveling-specific admin commands are the only guarded commands present.
    """
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123, admin_role_id=_ADMIN_ROLE_ID)
    store = LevelingStore(os.path.join(store_dir, "leveling.db"))
    svc = LevelingService(store)
    a.register_leveling_commands(svc)
    return a, svc, store


def _find_leveling_admin_commands(adapter):
    target = {"set-level-role", "remove-level-role", "list-level-roles"}
    return [c for c in adapter._client.pending_application_commands if c.name in target]


def test_leveling_admin_commands_non_admin_denied_store_unmodified():
    """Non-admin invokers of set/remove/list-level-role must receive the '관리자' denial
    response and must NOT mutate the reward store.

    Uses a real LevelingService + LevelingStore (not stubs).
    """
    with tempfile.TemporaryDirectory() as tmp:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            adapter, svc, store = _leveling_adapter_with_real_store(tmp)

            # Seed one mapping so list-level-roles has something to return IF auth passes
            svc.set_role_reward(123, 5, 888)

            cmds = _find_leveling_admin_commands(adapter)
            assert len(cmds) == 3, (
                f"Expected 3 leveling admin commands, found: {[c.name for c in cmds]}"
            )

            for cmd in cmds:
                # By-construction: the callback must carry the admin guard marker
                assert getattr(cmd.callback, "__gjc_admin_guarded__", False), (
                    f"{cmd.name} is not wrapped by admin_command InvokerCheck — "
                    "security regression: privileged command bypasses authz"
                )

                ctx = _Ctx(admin=False)
                loop.run_until_complete(cmd.callback(ctx))

                assert ctx.responses, f"{cmd.name}: no response emitted for non-admin caller"
                response_text = ctx.responses[-1][0]
                assert "관리자" in response_text, (
                    f"{cmd.name}: denial response must contain '관리자'; "
                    f"got {response_text!r}"
                )

            # Store must be untouched — only the seed entry remains
            assert svc.list_role_rewards(123) == [(5, 888)], (
                "Non-admin command callbacks must not mutate the reward store"
            )
        finally:
            asyncio.set_event_loop(None)
            loop.close()
            store.close()


def test_leveling_set_level_role_admin_can_mutate_store():
    """Positive control: admin caller on set-level-role successfully mutates the store.

    Verifies the gate passes for a legitimate admin and the store is updated.
    """
    with tempfile.TemporaryDirectory() as tmp:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            adapter, svc, store = _leveling_adapter_with_real_store(tmp)
            safe_role = types.SimpleNamespace(
                id=100, position=1, managed=False, name="safe-role",
                permissions=_perms(),
            )
            set_cmd = next(
                c for c in adapter._client.pending_application_commands
                if c.name == "set-level-role"
            )
            ctx = _Ctx(admin=True)
            # Provide guild so validate_reward_role can read bot.me.top_role
            ctx.guild = _guild_ns(bot_top=10)
            loop.run_until_complete(set_cmd.callback(ctx, level=3, role=safe_role))

            assert ctx.responses, "Admin set-level-role: expected a success response"
            resp_text = ctx.responses[-1][0]
            assert "레벨 3" in resp_text, f"Unexpected success response: {resp_text!r}"
            assert svc.list_role_rewards(123) == [(3, 100)], (
                "Admin set-level-role must persist the mapping"
            )
        finally:
            asyncio.set_event_loop(None)
            loop.close()
            store.close()


def test_leveling_set_level_role_dangerous_role_rejected_even_by_admin():
    """Admin calling set-level-role with a dangerous role must be rejected at the
    validate_reward_role gate — the mapping must NOT be persisted."""
    with tempfile.TemporaryDirectory() as tmp:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            adapter, svc, store = _leveling_adapter_with_real_store(tmp)
            dangerous_role = types.SimpleNamespace(
                id=777, position=1, managed=False, name="danger",
                permissions=_perms(administrator=True),
            )
            set_cmd = next(
                c for c in adapter._client.pending_application_commands
                if c.name == "set-level-role"
            )
            ctx = _Ctx(admin=True)
            ctx.guild = _guild_ns(bot_top=10)
            loop.run_until_complete(set_cmd.callback(ctx, level=1, role=dangerous_role))

            assert ctx.responses, "Expected a rejection response"
            resp_text = ctx.responses[-1][0]
            # The response must mention the rejection (안전하지 않아 거부)
            assert "거부" in resp_text, (
                f"Expected dangerous-role rejection text; got {resp_text!r}"
            )
            # Store must remain empty — dangerous role not persisted
            assert svc.list_role_rewards(123) == [], (
                "Dangerous role must not be persisted even when called by admin"
            )
        finally:
            asyncio.set_event_loop(None)
            loop.close()
            store.close()
