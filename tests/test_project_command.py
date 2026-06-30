"""S5 project-set tests (ralplan): /create-project composite op + partial-failure semantics.

Run: .venv/bin/python -m pytest tests/test_project_command.py -q
"""
import asyncio
import types

import pytest

from dcm.platform.pycord_adapter import PycordAdapter
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
    def __init__(self, fail_on_channel=None):
        self.calls = []
        self._n = 5000
        self._fail_on_channel = fail_on_channel  # nth create_channel (1-based) to raise on
        self._ch_count = 0

    def _id(self):
        self._n += 1
        return str(self._n)

    async def create_role(self, guild_id, name, *, permissions=0, reason):
        self.calls.append(("create_role", name, permissions))
        return self._id()

    async def create_category(self, guild_id, name, *, reason):
        self.calls.append(("create_category", name))
        return self._id()

    async def create_channel(self, guild_id, name, kind, category_id=None, *, reason):
        self._ch_count += 1
        if self._fail_on_channel and self._ch_count == self._fail_on_channel:
            raise RuntimeError(f"discord API 5xx on channel {name}")
        self.calls.append(("create_channel", name, kind, category_id))
        return self._id()

    async def set_channel_role_overwrite(self, guild_id, channel_id, role_id, *, view, reason):
        self.calls.append(("overwrite", channel_id, role_id, view))


class _Author:
    def __init__(self, admin, uid=42, name="choo"):
        self.roles = [types.SimpleNamespace(id=_ADMIN_ROLE_ID)] if admin else []
        self.id = uid
        self.display_name = name


class _Ctx:
    def __init__(self, admin=True, guild_id=123):
        self.author = _Author(admin)
        self.guild_id = guild_id
        self.responses = []

    async def respond(self, text, **kw):
        self.responses.append((text, kw))

    async def defer(self, **kw):
        self.deferred = True


def _adapter(fail_on_channel=None):
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123, admin_role_id=_ADMIN_ROLE_ID)
    fake = FakeGuildAdmin(fail_on_channel=fail_on_channel)
    a.register_admin_commands(GuildAdminService(fake, a.pending))
    return a, fake


def _cmd(a, name):
    return {c.name: c for c in a._client.pending_application_commands}[name]


def test_create_project_happy_path(loop):
    a, fake = _adapter()
    ctx = _Ctx()
    loop.run_until_complete(
        _cmd(a, "create-project").callback(ctx, name="알파", channels="general, voice-room", access_role="알파팀", confirm=True)
    )
    kinds = [c[0] for c in fake.calls]
    assert kinds.count("create_role") == 1
    assert kinds.count("create_category") == 1
    assert kinds.count("create_channel") == 2  # two channels parsed from CSV
    # each channel gets 2 overwrites: deny @everyone (role id == guild id) + allow access role
    overs = [c for c in fake.calls if c[0] == "overwrite"]
    assert len(overs) == 4
    assert any(o[2] == 123 and o[3] is False for o in overs)  # @everyone denied
    assert any(o[3] is True for o in overs)  # access role allowed
    assert ctx.responses[-1][1].get("ephemeral") is True and "완료" in ctx.responses[-1][0]


def test_create_project_requires_confirm(loop):
    a, fake = _adapter()
    ctx = _Ctx()
    loop.run_until_complete(
        _cmd(a, "create-project").callback(ctx, name="알파", channels="general", access_role="r", confirm=False)
    )
    assert fake.calls == []  # nothing created before confirm
    assert "confirm" in ctx.responses[-1][0]


def test_create_project_partial_failure_no_rollback(loop):
    a, fake = _adapter(fail_on_channel=2)  # 2nd channel creation blows up
    ctx = _Ctx()
    loop.run_until_complete(
        _cmd(a, "create-project").callback(ctx, name="알파", channels="a,b,c", access_role="r", confirm=True)
    )
    # role + category + first channel (and its overwrites) were created; then it stopped
    assert ("create_role", "r", 0) in fake.calls
    assert any(c[0] == "create_category" for c in fake.calls)
    assert sum(1 for c in fake.calls if c[0] == "create_channel") == 1  # only ch "a" before failure
    msg = ctx.responses[-1][0]
    assert "부분 실패" in msg and "롤백" in msg  # partial report, no auto-rollback


def test_create_project_non_admin_denied(loop):
    a, fake = _adapter()
    ctx = _Ctx(admin=False)
    loop.run_until_complete(
        _cmd(a, "create-project").callback(ctx, name="x", channels="a", access_role="r", confirm=True)
    )
    assert fake.calls == []
    assert "관리자" in ctx.responses[-1][0]


# --- overwrite primitive over a mock guild ---

class _FakeChannel:
    def __init__(self):
        self.set = None

    async def set_permissions(self, target, *, overwrite, reason):
        self.set = (target, overwrite.view_channel, reason)


def test_set_channel_role_overwrite_primitive(loop):
    ch = _FakeChannel()
    role = types.SimpleNamespace(id=9)
    guild = types.SimpleNamespace(get_channel=lambda cid: ch, get_role=lambda rid: role)
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123)
    a._client = types.SimpleNamespace(get_guild=lambda gid: guild)
    loop.run_until_complete(a.set_channel_role_overwrite(123, 55, 9, view=True, reason="grant"))
    assert ch.set == (role, True, "grant")
