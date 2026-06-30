"""S3 channel/category command tests (ralplan): command -> service -> GuildAdmin flow + primitives.

Run: .venv/bin/python -m pytest tests/test_channel_commands.py -q
Two layers, no live Discord:
  * command callbacks (mock ApplicationContext) over a fake-GuildAdmin-backed service — verifies
    option parsing, the actor/audit wiring, and the confirm-then-execute flow for /delete-channel;
  * adapter GuildAdmin primitives over a mock guild — verifies the right pycord calls + reason.
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
    def __init__(self):
        self.calls = []
        self._n = 2000

    def _id(self):
        self._n += 1
        return str(self._n)

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


def _adapter():
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123, admin_role_id=_ADMIN_ROLE_ID)
    fake = FakeGuildAdmin()
    a.register_admin_commands(GuildAdminService(fake, a.pending))
    return a, fake


def _cmd(a, name):
    return {c.name: c for c in a._client.pending_application_commands}[name]


# --- command -> service -> fake GuildAdmin ---

def test_create_category_command_flow(loop):
    a, fake = _adapter()
    ctx = _Ctx()
    loop.run_until_complete(_cmd(a, "create-category").callback(ctx, name="프로젝트"))
    call = fake.calls[-1]
    assert call[0] == "create_category" and call[1] == 123 and call[2] == "프로젝트"
    assert "choo" in call[-1] and "42" in call[-1]  # audit reason attributes the caller
    assert ctx.responses and ctx.responses[-1][1].get("ephemeral") is True


def test_create_channel_text_and_voice(loop):
    a, fake = _adapter()
    loop.run_until_complete(_cmd(a, "create-channel").callback(_Ctx(), name="general", kind="text"))
    assert fake.calls[-1][:4] == ("create_channel", 123, "general", "text")
    loop.run_until_complete(_cmd(a, "create-channel").callback(_Ctx(), name="회의실", kind="voice"))
    assert fake.calls[-1][:4] == ("create_channel", 123, "회의실", "voice")


def test_create_channel_rejects_bad_kind(loop):
    a, fake = _adapter()
    ctx = _Ctx()
    loop.run_until_complete(_cmd(a, "create-channel").callback(ctx, name="x", kind="stream"))
    assert not any(c[0] == "create_channel" for c in fake.calls)  # nothing created
    assert "text" in ctx.responses[-1][0] and "voice" in ctx.responses[-1][0]


def test_delete_channel_confirm_flow(loop):
    a, fake = _adapter()
    # without confirm -> asks, nothing deleted
    ctx1 = _Ctx()
    loop.run_until_complete(_cmd(a, "delete-channel").callback(ctx1, channel_id="55", confirm=False))
    assert not any(c[0] == "delete_channel" for c in fake.calls)
    assert "confirm" in ctx1.responses[-1][0]
    # with confirm=true -> issued token re-submitted, deleted
    ctx2 = _Ctx()
    loop.run_until_complete(_cmd(a, "delete-channel").callback(ctx2, channel_id="55", confirm=True))
    assert fake.calls[-1][0] == "delete_channel" and fake.calls[-1][1] == 123 and fake.calls[-1][2] == 55


def test_non_admin_denied_on_channel_command(loop):
    a, fake = _adapter()
    ctx = _Ctx(admin=False)
    loop.run_until_complete(_cmd(a, "create-category").callback(ctx, name="x"))
    assert fake.calls == []  # InvokerCheck blocked before the service
    assert "관리자" in ctx.responses[-1][0]


# --- adapter GuildAdmin primitives over a mock guild ---

class _FakeChannelObj:
    def __init__(self, cid):
        self.id = cid
        self.edited = None
        self.deleted_reason = None

    async def edit(self, *, reason, **fields):
        self.edited = (reason, fields)

    async def delete(self, *, reason):
        self.deleted_reason = reason


class _FakeGuild:
    def __init__(self):
        self.created = []
        self._channels = {}

    async def create_category(self, name, *, reason):
        self.created.append(("category", name, reason))
        return types.SimpleNamespace(id=900)

    async def create_text_channel(self, name, *, category=None, reason):
        self.created.append(("text", name, category, reason))
        return types.SimpleNamespace(id=901)

    async def create_voice_channel(self, name, *, category=None, reason):
        self.created.append(("voice", name, category, reason))
        return types.SimpleNamespace(id=902)

    def get_channel(self, cid):
        return self._channels.setdefault(cid, _FakeChannelObj(cid))


def _adapter_with_mock_guild(guild):
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123)
    a._client = types.SimpleNamespace(get_guild=lambda gid: guild)
    return a


def test_primitive_create_text_voice_category_carry_reason(loop):
    g = _FakeGuild()
    a = _adapter_with_mock_guild(g)
    assert loop.run_until_complete(a.create_category(123, "cat", reason="r1")) == "900"
    assert g.created[-1] == ("category", "cat", "r1")
    assert loop.run_until_complete(a.create_channel(123, "gen", "text", reason="r2")) == "901"
    assert g.created[-1][:2] == ("text", "gen") and g.created[-1][-1] == "r2"
    assert loop.run_until_complete(a.create_channel(123, "vc", "voice", reason="r3")) == "902"
    assert g.created[-1][:2] == ("voice", "vc") and g.created[-1][-1] == "r3"


def test_primitive_delete_and_edit_carry_reason(loop):
    g = _FakeGuild()
    a = _adapter_with_mock_guild(g)
    loop.run_until_complete(a.delete_channel(123, 55, reason="del-reason"))
    assert g.get_channel(55).deleted_reason == "del-reason"
    loop.run_until_complete(a.edit_channel(123, 55, name="newname", reason="edit-reason"))
    reason, fields = g.get_channel(55).edited
    assert reason == "edit-reason" and fields.get("name") == "newname"


def test_member_resolution_cache_then_fetch_fallback(loop):
    member = types.SimpleNamespace(id=7)
    fetched = {"n": 0}

    async def fetch_member(uid):
        fetched["n"] += 1
        return member

    guild = types.SimpleNamespace(get_member=lambda uid: None, fetch_member=fetch_member)
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123)
    a._client = types.SimpleNamespace(get_guild=lambda gid: guild)
    got = loop.run_until_complete(a._member(123, 7))
    assert got is member and fetched["n"] == 1  # cache miss -> fetch_member REST fallback
