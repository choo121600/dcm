"""S6 guard-completion tests (ralplan): error feedback, confirm button, rate limiter, token
sweep, empty-project validation, category-resolve raise. No live Discord.

Run: .venv/bin/python -m pytest tests/test_guards.py -q
"""
import asyncio
import types

import pytest

from dcm.platform.pycord_adapter import PycordAdapter, _ConfirmView
from dcm.service.guild_admin import GuildAdminService, PendingConfirmations, RateLimiter

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
        self._n = 6000

    def _id(self):
        self._n += 1
        return str(self._n)

    async def create_category(self, guild_id, name, *, reason):
        self.calls.append(("create_category", name))
        return self._id()

    async def create_role(self, guild_id, name, *, permissions=0, reason):
        self.calls.append(("create_role", name))
        return self._id()

    async def create_channel(self, guild_id, name, kind, category_id=None, *, reason):
        self.calls.append(("create_channel", name))
        return self._id()

    async def set_channel_role_overwrite(self, guild_id, channel_id, role_id, *, view, reason):
        self.calls.append(("overwrite", channel_id, role_id, view))

    async def delete_channel(self, guild_id, channel_id, *, reason):
        self.calls.append(("delete_channel", channel_id))


class _Author:
    def __init__(self, admin=True):
        self.roles = [types.SimpleNamespace(id=_ADMIN_ROLE_ID)] if admin else []
        self.id = 42
        self.display_name = "choo"


class _Ctx:
    def __init__(self, admin=True, guild_id=123):
        self.author = _Author(admin)
        self.guild_id = guild_id
        self.responses = []

    async def respond(self, text, **kw):
        self.responses.append((text, kw))

    async def defer(self, **kw):
        self.deferred = True


class _Interaction:
    def __init__(self):
        self.edited = None
        self.deferred = False
        self.response = types.SimpleNamespace(defer=self._defer)

    async def _defer(self):
        self.deferred = True

    async def edit_original_response(self, *, content, view):
        self.edited = (content, view)


def _adapter():
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123, admin_role_id=_ADMIN_ROLE_ID)
    fake = FakeGuildAdmin()
    a.register_admin_commands(GuildAdminService(fake, a.pending))
    return a, fake


def _cmd(a, name):
    return {c.name: c for c in a._client.pending_application_commands}[name]


# --- A: error feedback on the slash path ---

def test_bad_input_surfaces_ephemeral_error_not_unhandled(loop):
    a, fake = _adapter()
    ctx = _Ctx()
    # non-numeric channel id -> int() ValueError inside the factory -> caught, ephemeral error
    loop.run_until_complete(_cmd(a, "delete-channel").callback(ctx, channel_id="not-a-number", confirm=True))
    assert ctx.responses and ctx.responses[-1][1].get("ephemeral") is True
    assert "오류" in ctx.responses[-1][0]
    assert not any(c[0] == "delete_channel" for c in fake.calls)


# --- F: confirm button view ---

def test_high_risk_renders_confirm_button_and_button_executes(loop):
    a, fake = _adapter()
    ctx = _Ctx()
    loop.run_until_complete(_cmd(a, "delete-channel").callback(ctx, channel_id="55", confirm=False))
    # a confirm View was attached to the (ephemeral) response, nothing deleted yet
    view = ctx.responses[-1][1].get("view")
    assert isinstance(view, _ConfirmView)
    assert not any(c[0] == "delete_channel" for c in fake.calls)
    # clicking the confirm button executes the op (token consumed in the policy layer)
    interaction = _Interaction()
    loop.run_until_complete(view._do_confirm(interaction))
    assert any(c == ("delete_channel", 55) for c in fake.calls)
    assert interaction.edited is not None and all(item.disabled for item in view.children)


# --- B: additive rate limiter ---

def test_rate_limiter_spaces_calls(loop):
    rl = RateLimiter(min_interval=0.05)
    slept = []
    clock = [0.0]

    async def fake_sleep(d):
        slept.append(d)

    async def run():
        w1 = await rl.acquire(now=lambda: clock[0], sleep=fake_sleep)
        w2 = await rl.acquire(now=lambda: clock[0], sleep=fake_sleep)  # same instant -> must wait
        return w1, w2

    w1, w2 = loop.run_until_complete(run())
    assert w1 == 0.0  # first call never waits
    assert w2 == pytest.approx(0.05) and slept == [pytest.approx(0.05)]


# --- C: pending-confirmation token sweep ---

def test_pending_confirmations_sweeps_expired_on_register():
    p = PendingConfirmations(ttl_seconds=100)
    old = p.register("old", now=0)
    p.register("new", now=1000)  # registering sweeps tokens older than ttl
    assert p.consume(old, now=1000) is False  # the stale token was swept out


# --- D: empty project validation ---

def test_create_project_rejects_empty_channels(loop):
    a, fake = _adapter()
    ctx = _Ctx()
    loop.run_until_complete(
        _cmd(a, "create-project").callback(ctx, name="알파", channels="   ", access_role="t", confirm=True)
    )
    assert fake.calls == []  # nothing created
    assert "최소 1개" in ctx.responses[-1][0]


# --- E: category-resolve raises instead of silent fallback ---

def test_create_channel_primitive_raises_on_unresolved_category(loop):
    guild = types.SimpleNamespace(get_channel=lambda cid: None)  # category never resolves
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123)
    a._client = types.SimpleNamespace(get_guild=lambda gid: guild)
    with pytest.raises(RuntimeError, match="category 999 not found"):
        loop.run_until_complete(a.create_channel(123, "x", "text", category_id=999, reason="r"))


# --- defer: ack within Discord's 3s window before bulk REST work (S6 WATCH fix) ---

def test_command_defers_before_work(loop):
    a, fake = _adapter()
    ctx = _Ctx()
    loop.run_until_complete(_cmd(a, "create-category").callback(ctx, name="x"))
    assert getattr(ctx, "deferred", False) is True  # acked before the REST call


def test_confirm_button_defers_interaction(loop):
    a, fake = _adapter()
    ctx = _Ctx()
    loop.run_until_complete(_cmd(a, "delete-channel").callback(ctx, channel_id="55", confirm=False))
    view = ctx.responses[-1][1].get("view")
    interaction = _Interaction()
    loop.run_until_complete(view._do_confirm(interaction))
    assert interaction.deferred is True and interaction.edited is not None
