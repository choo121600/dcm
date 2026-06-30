"""S4 role command tests (ralplan): command -> service -> GuildAdmin flow + role primitives.

Run: .venv/bin/python -m pytest tests/test_role_commands.py -q
No live Discord: command callbacks over a fake-GuildAdmin-backed service (incl. dangerous-role
confirm gating), and adapter role primitives over a mock guild.
"""
import asyncio
import types

import pytest

from dcm.platform.pycord_adapter import PycordAdapter
from dcm.service import guild_admin as ga
from dcm.service.guild_admin import GuildAdminService

_ADMIN_ROLE_ID = 999


@pytest.fixture(autouse=True)
def loop():
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    asyncio.set_event_loop(None)
    lp.close()


class FakeGuildAdmin:
    def __init__(self, role_perms=None):
        self.calls = []
        self._role_perms = role_perms or {}
        self._n = 3000

    def _id(self):
        self._n += 1
        return str(self._n)

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


class _Author:
    def __init__(self, admin, uid, name):
        self.roles = [types.SimpleNamespace(id=_ADMIN_ROLE_ID)] if admin else []
        self.id = uid
        self.display_name = name


class _Ctx:
    def __init__(self, admin=True, uid=42, name="choo", guild_id=123):
        self.author = _Author(admin, uid, name)
        self.guild_id = guild_id
        self.responses = []

    async def respond(self, text, **kw):
        self.responses.append((text, kw))

    async def defer(self, **kw):
        self.deferred = True


def _adapter(role_perms=None):
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123, admin_role_id=_ADMIN_ROLE_ID)
    fake = FakeGuildAdmin(role_perms=role_perms)
    a.register_admin_commands(GuildAdminService(fake, a.pending))
    return a, fake


def _cmd(a, name):
    return {c.name: c for c in a._client.pending_application_commands}[name]


def test_create_role_safe_direct_dangerous_confirms(loop):
    a, fake = _adapter()
    loop.run_until_complete(_cmd(a, "create-role").callback(_Ctx(), name="member", permissions=0))
    assert fake.calls[-1][0] == "create_role" and fake.calls[-1][3] == 0
    # dangerous perms -> needs confirm
    ctx = _Ctx()
    loop.run_until_complete(
        _cmd(a, "create-role").callback(ctx, name="mod", permissions=ga.MANAGE_ROLES, confirm=False)
    )
    assert not any(c[0] == "create_role" and c[2] == "mod" for c in fake.calls)
    assert "confirm" in ctx.responses[-1][0]
    # confirm -> created
    loop.run_until_complete(
        _cmd(a, "create-role").callback(_Ctx(), name="mod", permissions=ga.MANAGE_ROLES, confirm=True)
    )
    assert any(c[0] == "create_role" and c[2] == "mod" for c in fake.calls)


def test_assign_role_dangerous_confirms_safe_direct(loop):
    a, fake = _adapter(role_perms={9: ga.MANAGE_CHANNELS, 3: 0})
    # dangerous role -> needs confirm
    ctx = _Ctx()
    loop.run_until_complete(_cmd(a, "assign-role").callback(ctx, user_id="7", role_id="9", confirm=False))
    assert not any(c[0] == "assign_role" for c in fake.calls)
    assert "confirm" in ctx.responses[-1][0]
    # confirm -> assigned
    loop.run_until_complete(_cmd(a, "assign-role").callback(_Ctx(), user_id="7", role_id="9", confirm=True))
    assert any(c[0] == "assign_role" and c[2] == 7 and c[3] == 9 for c in fake.calls)
    # safe role -> direct
    loop.run_until_complete(_cmd(a, "assign-role").callback(_Ctx(), user_id="7", role_id="3"))
    assert fake.calls[-1][0] == "assign_role" and fake.calls[-1][3] == 3


def test_remove_role_direct(loop):
    a, fake = _adapter()
    loop.run_until_complete(_cmd(a, "remove-role").callback(_Ctx(), user_id="7", role_id="5"))
    assert fake.calls[-1][:4] == ("remove_role", 123, 7, 5)


def test_set_role_permissions_dangerous_confirms(loop):
    a, fake = _adapter()
    ctx = _Ctx()
    loop.run_until_complete(
        _cmd(a, "set-role-permissions").callback(ctx, role_id="5", permissions=ga.ADMINISTRATOR, confirm=False)
    )
    assert not any(c[0] == "set_role_permissions" for c in fake.calls)
    loop.run_until_complete(
        _cmd(a, "set-role-permissions").callback(_Ctx(), role_id="5", permissions=ga.ADMINISTRATOR, confirm=True)
    )
    assert fake.calls[-1][0] == "set_role_permissions" and fake.calls[-1][2] == 5


def test_non_admin_denied_on_role_command(loop):
    a, fake = _adapter()
    ctx = _Ctx(admin=False)
    loop.run_until_complete(_cmd(a, "create-role").callback(ctx, name="x", permissions=0))
    assert fake.calls == []
    assert "관리자" in ctx.responses[-1][0]


# --- adapter role primitives over a mock guild ---

class _FakeRole:
    def __init__(self, rid, perms=0):
        self.id = rid
        self.permissions = types.SimpleNamespace(value=perms)
        self.edited = None

    async def edit(self, *, permissions, reason):
        self.edited = (permissions.value, reason)


class _FakeMember:
    def __init__(self):
        self.added = None
        self.removed = None

    async def add_roles(self, role, *, reason):
        self.added = (role.id, reason)

    async def remove_roles(self, role, *, reason):
        self.removed = (role.id, reason)


class _RoleGuild:
    def __init__(self, member, roles):
        self._member = member
        self._roles = roles
        self.created = None

    async def create_role(self, *, name, permissions, reason):
        self.created = (name, permissions.value, reason)
        return types.SimpleNamespace(id=950)

    def get_role(self, rid):
        return self._roles.get(rid)

    def get_member(self, uid):
        return self._member


def _adapter_mock_guild(guild):
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123)
    a._client = types.SimpleNamespace(get_guild=lambda gid: guild)
    return a


def test_role_primitives_carry_reason(loop):
    member, role = _FakeMember(), _FakeRole(5, perms=0)
    g = _RoleGuild(member, {5: role})
    a = _adapter_mock_guild(g)
    rid = loop.run_until_complete(a.create_role(123, "mod", permissions=ga.MANAGE_ROLES, reason="r"))
    assert rid == "950" and g.created == ("mod", ga.MANAGE_ROLES, "r")
    assert loop.run_until_complete(a.role_permissions(123, 5)) == 0
    loop.run_until_complete(a.assign_role(123, 7, 5, reason="assign-r"))
    assert member.added == (5, "assign-r")
    loop.run_until_complete(a.remove_role(123, 7, 5, reason="remove-r"))
    assert member.removed == (5, "remove-r")
    loop.run_until_complete(a.set_role_permissions(123, 5, ga.MANAGE_CHANNELS, reason="perm-r"))
    assert role.edited == (ga.MANAGE_CHANNELS, "perm-r")
