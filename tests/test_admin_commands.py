"""S2 adapter tests (ralplan): InvokerCheck registration wrapper, /whoami, by-construction guard.

Run: .venv/bin/python -m pytest tests/test_admin_commands.py -q
Proves the SOLE authz boundary is enforced *by construction*: every registered application
command carries the Manage Guild InvokerCheck, non-admins are denied, admins pass. Uses the
real discord.Bot for registration and a mock ApplicationContext (no live gateway).
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


class _Author:
    def __init__(self, admin: bool) -> None:
        self.roles = [types.SimpleNamespace(id=_ADMIN_ROLE_ID)] if admin else []


class _Ctx:
    """Minimal pycord ApplicationContext stand-in (only what InvokerCheck/whoami read)."""

    def __init__(self, admin: bool = True) -> None:
        self.author = _Author(admin)
        self.responses: list[tuple] = []

    async def respond(self, text, **kw):
        self.responses.append((text, kw))


def _adapter() -> PycordAdapter:
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123, admin_role_id=_ADMIN_ROLE_ID)
    a.register_admin_commands(GuildAdminService(a, a.pending))
    return a


def _command(adapter: PycordAdapter, name: str):
    return {c.name: c for c in adapter._client.pending_application_commands}[name]


def test_whoami_registered(loop):
    a = _adapter()
    names = [c.name for c in a._client.pending_application_commands]
    assert "whoami" in names


def test_every_command_is_invokercheck_guarded_by_construction(loop):
    """If any slash command were registered outside admin_command, it would lack the marker."""
    a = _adapter()
    cmds = list(a._client.pending_application_commands)
    assert cmds, "expected at least the /whoami command registered"
    for cmd in cmds:
        assert getattr(cmd.callback, "__gjc_admin_guarded__", False), (
            f"{cmd.name} bypassed the InvokerCheck registration wrapper"
        )


def test_non_admin_is_denied(loop):
    a = _adapter()
    whoami = _command(a, "whoami")
    ctx = _Ctx(admin=False)
    loop.run_until_complete(whoami.callback(ctx))
    assert ctx.responses, "no response sent"
    text, kw = ctx.responses[-1]
    assert kw.get("ephemeral") is True
    assert "관리자" in text  # denial message names the admin requirement


def test_admin_is_allowed(loop):
    a = _adapter()
    whoami = _command(a, "whoami")
    ctx = _Ctx(admin=True)
    loop.run_until_complete(whoami.callback(ctx))
    assert ctx.responses, "no response sent"
    text, kw = ctx.responses[-1]
    assert kw.get("ephemeral") is True
    assert "확인" in text  # success path reached only when InvokerCheck passes
