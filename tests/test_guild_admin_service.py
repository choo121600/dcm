"""S2 service-layer tests (ralplan): policy + GuildAdminService over a fake GuildAdmin.

Run: .venv/bin/python -m pytest tests/test_guild_admin_service.py -q
The service layer is exercised with NO discord import (DIP on the GuildAdmin protocol);
a subprocess import-isolation check enforces that invariant as a hard gate.
"""
import asyncio
import subprocess
import sys

from dcm.service import guild_admin as ga
from dcm.service.guild_admin import GuildAdminService, PendingConfirmations


class FakeGuildAdmin:
    """In-memory GuildAdmin double — records calls, returns ids; no discord."""

    def __init__(self, role_perms: dict | None = None) -> None:
        self.calls: list[tuple] = []
        self._role_perms = role_perms or {}
        self._next = 1000
        self.existing_roles: list[dict] = []  # list_roles 결과 (멱등성 테스트용)
        self.existing_channels: list[dict] = []  # list_channels 결과

    def _id(self) -> str:
        self._next += 1
        return str(self._next)

    async def create_category(self, guild_id, name, *, reason):
        self.calls.append(("create_category", guild_id, name, reason))
        return self._id()

    async def create_channel(self, guild_id, name, kind, category_id=None, *, reason):
        self.calls.append(("create_channel", guild_id, name, kind, category_id, reason))
        return self._id()

    async def edit_channel(self, guild_id, channel_id, *, name=None, category_id=None, reason):
        self.calls.append(("edit_channel", guild_id, channel_id, name, category_id, reason))

    async def delete_channel(self, guild_id, channel_id, *, reason):
        self.calls.append(("delete_channel", guild_id, channel_id, reason))

    async def create_role(self, guild_id, name, *, permissions=0, reason):
        self.calls.append(("create_role", guild_id, name, permissions, reason))
        return self._id()

    async def role_permissions(self, guild_id, role_id):
        return self._role_perms.get(role_id, 0)

    async def assign_role(self, guild_id, user_id, role_id, *, reason):
        self.calls.append(("assign_role", guild_id, user_id, role_id, reason))

    async def remove_role(self, guild_id, user_id, role_id, *, reason):
        self.calls.append(("remove_role", guild_id, user_id, role_id, reason))

    async def set_role_permissions(self, guild_id, role_id, permissions, *, reason):
        self.calls.append(("set_role_permissions", guild_id, role_id, permissions, reason))

    async def set_channel_role_overwrite(self, guild_id, channel_id, role_id, *, view, reason):
        self.calls.append(("overwrite", guild_id, channel_id, role_id, view, reason))

    async def list_roles(self, guild_id):
        return list(self.existing_roles)

    async def list_channels(self, guild_id):
        return list(self.existing_channels)


def _svc(role_perms=None):
    admin = FakeGuildAdmin(role_perms=role_perms)
    return GuildAdminService(admin, PendingConfirmations()), admin


def test_role_is_dangerous():
    assert ga.role_is_dangerous(ga.MANAGE_ROLES)
    assert ga.role_is_dangerous(ga.MANAGE_CHANNELS)
    assert ga.role_is_dangerous(ga.ADMINISTRATOR)
    assert not ga.role_is_dangerous(0)
    assert not ga.role_is_dangerous(1 << 11)  # SEND_MESSAGES is not a management permission


def test_classify_risk():
    assert ga.classify_risk("create_channel") == ga.RISK_LOW
    assert ga.classify_risk("delete_channel") == ga.RISK_HIGH
    assert ga.classify_risk("create_project") == ga.RISK_HIGH
    assert ga.classify_risk("create_role", role_permissions=0) == ga.RISK_LOW
    assert ga.classify_risk("create_role", role_permissions=ga.MANAGE_ROLES) == ga.RISK_HIGH


def test_audit_reason_clamped_and_attributes_human():
    long = ga.audit_reason("a" * 1000, 42, "x" * 1000)
    assert len(long) <= ga.AUDIT_REASON_MAX
    assert "42" in ga.audit_reason("choo", 42, "create channel") and "choo" in ga.audit_reason("choo", 42, "x")


def test_pending_confirmations_single_use_and_expiry():
    p = PendingConfirmations()
    tok = p.register("delete #x", now=0)
    assert p.consume(tok, now=1) is True
    assert p.consume(tok, now=2) is False  # single-use
    tok2 = p.register("delete #y", now=0)
    assert p.consume(tok2, now=10_000) is False  # expired beyond ttl
    assert p.consume("never-issued") is False


def test_create_channel_low_risk_executes_with_audit_reason():
    svc, admin = _svc()
    res = asyncio.run(
        svc.create_channel(guild_id=1, actor_name="choo", actor_id=42, name="general", kind="text")
    )
    assert res.ok and not res.needs_confirmation
    call = admin.calls[-1]
    assert call[0] == "create_channel" and call[2] == "general" and call[3] == "text"
    assert "choo" in call[-1] and "42" in call[-1]  # X-Audit-Log-Reason attributes the human caller


def test_delete_channel_requires_confirmation_then_executes():
    svc, admin = _svc()
    first = asyncio.run(svc.delete_channel(guild_id=1, actor_name="choo", actor_id=42, channel_id=55))
    assert first.needs_confirmation and first.confirmation_token and not first.ok
    assert not any(c[0] == "delete_channel" for c in admin.calls)  # nothing deleted before confirm
    second = asyncio.run(
        svc.delete_channel(guild_id=1, actor_name="choo", actor_id=42, channel_id=55, confirm_token=first.confirmation_token)
    )
    assert second.ok and admin.calls[-1][0] == "delete_channel"
    bogus = asyncio.run(
        svc.delete_channel(
            guild_id=1, actor_name="choo", actor_id=42, channel_id=55, confirm_token="bogus"
        )
    )
    assert bogus.needs_confirmation and not bogus.ok  # invalid/expired token refused in the policy layer


def test_create_role_dangerous_requires_confirmation():
    svc, admin = _svc()
    safe = asyncio.run(
        svc.create_role(guild_id=1, actor_name="choo", actor_id=42, name="member", permissions=0)
    )
    assert safe.ok and admin.calls[-1][0] == "create_role"
    danger = asyncio.run(
        svc.create_role(
            guild_id=1, actor_name="choo", actor_id=42, name="mod", permissions=ga.MANAGE_ROLES
        )
    )
    assert danger.needs_confirmation and not danger.ok
    confirmed = asyncio.run(
        svc.create_role(
            guild_id=1,
            actor_name="choo",
            actor_id=42,
            name="mod",
            permissions=ga.MANAGE_ROLES,
            confirm_token=danger.confirmation_token,
        )
    )
    assert confirmed.ok and confirmed.risk == ga.RISK_HIGH


def test_assign_dangerous_role_requires_confirmation_safe_role_direct():
    svc, _ = _svc(role_perms={9: ga.MANAGE_CHANNELS})
    res = asyncio.run(svc.assign_role(guild_id=1, actor_name="choo", actor_id=42, user_id=7, role_id=9))
    assert res.needs_confirmation
    svc2, admin2 = _svc(role_perms={3: 0})
    ok = asyncio.run(svc2.assign_role(guild_id=1, actor_name="choo", actor_id=42, user_id=7, role_id=3))
    assert ok.ok and admin2.calls[-1][0] == "assign_role"


def test_service_and_orchestrator_never_import_discord():
    """Hard import-isolation gate (ralplan S2): a fresh interpreter importing the service +
    orchestrator must NOT pull discord into sys.modules (discord lives only in platform/)."""
    code = (
        "import dcm.service.guild_admin, dcm.orchestrator, sys; "
        "bad=sorted(m for m in sys.modules if m=='discord' or m.startswith('discord.')); "
        "assert not bad, bad"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# --- apply_template (서버 블루프린트) ---

_TEMPLATE = """
roles:
  - name: 운영진
    permissions: [manage_channels]
  - name: 멤버
categories:
  - name: 2026-summer
    private: true
    visible_to: [운영진]
    channels:
      - name: 공지
        type: text
      - name: 회의
        type: voice
"""


def _run(coro):
    return asyncio.run(coro)


def test_apply_template_dryrun_then_confirm_creates():
    svc, admin = _svc()
    dry = _run(svc.apply_template(guild_id=1, actor_name="a", actor_id=9, template_text=_TEMPLATE))
    assert dry.needs_confirmation and dry.confirmation_token and not dry.ok
    assert admin.calls == [], "드라이런은 아무것도 생성하지 않아야 함"
    done = _run(
        svc.apply_template(guild_id=1, actor_name="a", actor_id=9, confirm_token=dry.confirmation_token)
    )
    assert done.ok, done.detail
    kinds = [c[0] for c in admin.calls]
    assert kinds.count("create_role") == 2
    assert kinds.count("create_category") == 1
    assert kinds.count("create_channel") == 2
    assert any(c[0] == "overwrite" for c in admin.calls), "private 카테고리 overwrite 누락"


def test_apply_template_invalid_token_refused():
    svc, admin = _svc()
    res = _run(svc.apply_template(guild_id=1, actor_name="a", actor_id=9, confirm_token="bogus"))
    assert not res.ok and res.needs_confirmation
    assert admin.calls == []


def test_apply_template_bad_template_no_creation():
    svc, admin = _svc()
    res = _run(
        svc.apply_template(
            guild_id=1, actor_name="a", actor_id=9, template_text="roles:\n  - permissions: [kick_members]\n"
        )
    )
    assert not res.ok and not res.needs_confirmation
    assert "템플릿 오류" in res.detail
    assert admin.calls == []


def test_apply_template_idempotent_skips_existing():
    svc, admin = _svc()
    admin.existing_roles = [{"id": "500", "name": "운영진"}]
    admin.existing_channels = [
        {"id": "600", "name": "2026-summer", "type": 4, "parent_id": None},
        {"id": "601", "name": "공지", "type": 0, "parent_id": "600"},
    ]
    dry = _run(svc.apply_template(guild_id=1, actor_name="a", actor_id=9, template_text=_TEMPLATE))
    _run(svc.apply_template(guild_id=1, actor_name="a", actor_id=9, confirm_token=dry.confirmation_token))
    created_roles = [c[2] for c in admin.calls if c[0] == "create_role"]
    created_chans = [(c[2], c[3]) for c in admin.calls if c[0] == "create_channel"]
    cats = [c for c in admin.calls if c[0] == "create_category"]
    assert created_roles == ["멤버"], "이미 있는 운영진은 건너뛰어야"
    assert cats == [], "이미 있는 카테고리는 재생성 금지"
    assert created_chans == [("회의", "voice")], "이미 있는 공지는 건너뛰고 회의만 생성"
