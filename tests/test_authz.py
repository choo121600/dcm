"""S2 authz tests (ralplan): AuthContext/TargetRef contracts, the is_admin predicate,
role_ids boundary carry, adapter role resolution, and by-construction InvokerCheck
rejection across the FULL registered admin-command verb set.

The "by-construction" guarantee: every privileged command routes through admin_command,
whose wrapper runs the InvokerCheck (admin_role_id membership) before the body. Enumerating
the framework registry (pending_application_commands) and asserting each rejects a non-admin
caller proves no privileged path bypasses the single authz chokepoint.

Run: .venv/bin/python -m pytest tests/test_authz.py -q
"""
import asyncio
import types

import pytest

from dcm.platform.base import AuthContext, IncomingMessage, TargetRef, is_admin
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


# --- pure boundary contracts -------------------------------------------------

def test_is_admin_predicate():
    assert is_admin(frozenset({999}), 999) is True
    assert is_admin(frozenset({1, 2, 999}), 999) is True
    assert is_admin(frozenset({1, 2}), 999) is False
    assert is_admin(frozenset(), 999) is False
    # admin_role_id unset (0) denies by construction even if 0 somehow appears in roles
    assert is_admin(frozenset({0}), 0) is False


def test_authcontext_is_frozen_plain_data():
    a = AuthContext(author_id="42", author_name="choo", role_ids=frozenset({999}))
    assert a.role_ids == frozenset({999})
    with pytest.raises(Exception):
        a.author_id = "x"  # frozen dataclass


def test_targetref_carries_human_label():
    t = TargetRef(id="123", label="#general")
    assert (t.id, t.label) == ("123", "#general")


def test_incoming_message_role_ids_default_and_set():
    m = IncomingMessage(channel_id="1", author_id="2", author_name="n", content="hi")
    assert m.role_ids == frozenset()  # default empty → unauthorized for privileged ops
    m2 = IncomingMessage(channel_id="1", author_id="2", author_name="n", content="hi",
                         role_ids=frozenset({999}))
    assert m2.role_ids == frozenset({999})


# --- adapter resolves Discord roles to plain-data role_ids -------------------

def test_adapter_role_ids_resolution():
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123, admin_role_id=_ADMIN_ROLE_ID)
    member = types.SimpleNamespace(
        roles=[types.SimpleNamespace(id=999), types.SimpleNamespace(id=7)]
    )
    assert a._role_ids(member) == frozenset({999, 7})
    # graceful on missing / None roles
    assert a._role_ids(types.SimpleNamespace()) == frozenset()
    assert a._role_ids(types.SimpleNamespace(roles=None)) == frozenset()


# --- by-construction InvokerCheck over the whole verb set -------------------

class _Author:
    def __init__(self, admin):
        self.roles = [types.SimpleNamespace(id=_ADMIN_ROLE_ID)] if admin else []
        self.id = 42
        self.display_name = "choo"


class _Ctx:
    def __init__(self, admin):
        self.author = _Author(admin)
        self.guild_id = 123
        self.responses = []

    async def respond(self, text, **kw):
        self.responses.append((text, kw))


def _adapter():
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123, admin_role_id=_ADMIN_ROLE_ID)
    a.register_admin_commands(GuildAdminService(a, a.pending))
    return a


def _guarded_commands(a):
    cmds = list(a._client.pending_application_commands)
    assert cmds, "no admin commands registered"
    for c in cmds:
        assert getattr(c.callback, "__gjc_admin_guarded__", False), (
            f"{c.name} bypassed the admin_command InvokerCheck wrapper"
        )
    return cmds


def test_every_admin_command_rejects_non_admin_by_construction(loop):
    a = _adapter()
    cmds = _guarded_commands(a)
    assert set(a._admin_commands) == {c.name for c in cmds}
    for cmd in cmds:
        ctx = _Ctx(admin=False)
        loop.run_until_complete(cmd.callback(ctx))
        assert ctx.responses, f"{cmd.name}: no denial response"
        assert "관리자" in ctx.responses[-1][0], f"{cmd.name} did not reject a non-admin caller"


def test_admin_role_holder_passes_invokercheck(loop):
    a = _adapter()
    whoami = next(c for c in _guarded_commands(a) if c.name == "whoami")
    ctx = _Ctx(admin=True)
    loop.run_until_complete(whoami.callback(ctx))
    assert ctx.responses and "확인" in ctx.responses[-1][0]


def test_some_role_but_not_admin_role_is_rejected(loop):
    # Holding a role that is NOT the designated ADMIN_ROLE must still be rejected
    # (authz is bound to the specific admin role id, not "has any role").
    a = _adapter()
    whoami = next(c for c in _guarded_commands(a) if c.name == "whoami")
    ctx = _Ctx(admin=False)
    ctx.author.roles = [types.SimpleNamespace(id=12345)]
    loop.run_until_complete(whoami.callback(ctx))
    assert "관리자" in ctx.responses[-1][0]

# --- guild owner is always admin (Discord owners inherently hold all perms) ---

def test_is_admin_owner_bypass():
    # The guild owner is always admin — regardless of roles or whether admin_role is set.
    assert is_admin(frozenset(), 999, is_owner=True) is True
    assert is_admin(frozenset(), 0, is_owner=True) is True
    assert is_admin(frozenset({1, 2}), 999, is_owner=True) is True
    # Not the owner and lacking the admin role → still denied (default is_owner=False).
    assert is_admin(frozenset({1, 2}), 999) is False
    assert is_admin(frozenset({1, 2}), 999, is_owner=False) is False


def test_authcontext_and_incoming_carry_is_owner():
    assert AuthContext(author_id="1", author_name="n").is_owner is False
    assert AuthContext(author_id="1", author_name="n", is_owner=True).is_owner is True
    base = IncomingMessage(channel_id="1", author_id="1", author_name="n", content="c")
    assert base.is_owner is False
    owned = IncomingMessage(
        channel_id="1", author_id="1", author_name="n", content="c", is_owner=True
    )
    assert owned.is_owner is True


def test_adapter_is_owner_detection():
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123, admin_role_id=_ADMIN_ROLE_ID)
    owner = types.SimpleNamespace(
        guild=types.SimpleNamespace(owner_id=42), author=types.SimpleNamespace(id=42)
    )
    assert a._is_owner(owner) is True
    not_owner = types.SimpleNamespace(
        guild=types.SimpleNamespace(owner_id=7), author=types.SimpleNamespace(id=42)
    )
    assert a._is_owner(not_owner) is False
    # DM / no guild → never owner
    assert a._is_owner(types.SimpleNamespace(author=types.SimpleNamespace(id=42))) is False
    # slash ApplicationContext exposes the caller as .user, not .author
    via_user = types.SimpleNamespace(
        guild=types.SimpleNamespace(owner_id=42), user=types.SimpleNamespace(id=42)
    )
    assert a._is_owner(via_user) is True


def test_guild_owner_passes_invokercheck_without_admin_role(loop):
    # The server owner must pass even with NO admin role assigned.
    a = _adapter()
    whoami = next(c for c in _guarded_commands(a) if c.name == "whoami")
    ctx = _Ctx(admin=False)  # author.id == 42, roles == []
    ctx.guild = types.SimpleNamespace(owner_id=42)  # caller IS the guild owner
    loop.run_until_complete(whoami.callback(ctx))
    assert ctx.responses and "확인" in ctx.responses[-1][0], "guild owner was wrongly rejected"


def test_non_owner_without_admin_role_still_rejected(loop):
    a = _adapter()
    whoami = next(c for c in _guarded_commands(a) if c.name == "whoami")
    ctx = _Ctx(admin=False)
    ctx.guild = types.SimpleNamespace(owner_id=99999)  # someone else owns the guild
    loop.run_until_complete(whoami.callback(ctx))
    assert "관리자" in ctx.responses[-1][0]


# --- per-guild authz (멀티길드 P3): Manage Guild 폴백은 관리역할 미설정 길드 한정 ---

def test_is_admin_manage_guild_fallback_only_when_role_unset():
    # 관리역할 미설정(0) 길드: 디스코드 Manage Guild/Administrator 폴백 통과
    assert is_admin(frozenset(), 0, has_manage_guild=True) is True
    # 관리역할 설정(999) 길드: Manage Guild만 있고 역할 없으면 거부 (G1 핵심 — 기존 보안 불변)
    assert is_admin(frozenset(), 999, has_manage_guild=True) is False
    # 설정 길드 + 역할 보유 → 통과
    assert is_admin(frozenset({999}), 999, has_manage_guild=False) is True
    # 미설정 + 권한 없음 → 거부
    assert is_admin(frozenset(), 0, has_manage_guild=False) is False
    # owner 는 무엇이든 통과
    assert is_admin(frozenset(), 0, is_owner=True, has_manage_guild=False) is True


def test_adapter_resolves_per_guild_admin_role():
    import types

    class _FakeSettings:
        def __init__(self, mapping):
            self._m = mapping

        def get(self, gid):
            from dcm.service.guild_settings import GuildSettings

            return GuildSettings(guild_id=str(gid), admin_role_id=self._m.get(int(gid), 0))

    a = PycordAdapter(
        token="x", bot_name="지우", guild_id=123, admin_role_id=999, guild_settings=_FakeSettings({100: 555})
    )
    assert a._guild_admin_role(100) == 555  # 설정된 길드 → 그 역할
    assert a._guild_admin_role(200) == 0  # 미설정 길드 → 0(=Manage Guild 폴백 트리거)
    # has_manage_guild 해석
    assert a._has_manage_guild(types.SimpleNamespace(guild_permissions=types.SimpleNamespace(manage_guild=True, administrator=False))) is True
    assert a._has_manage_guild(types.SimpleNamespace(guild_permissions=types.SimpleNamespace(manage_guild=False, administrator=True))) is True
    assert a._has_manage_guild(types.SimpleNamespace(guild_permissions=types.SimpleNamespace(manage_guild=False, administrator=False))) is False
    assert a._has_manage_guild(types.SimpleNamespace()) is False


def test_adapter_no_settings_falls_back_to_env_admin_role():
    # settings 미주입(단일길드 호환): env 시드 admin_role_id 사용
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123, admin_role_id=999)
    assert a._guild_admin_role(123) == 999
    assert a._guild_admin_role(456) == 999  # settings 없으면 모든 길드가 env 값


# --- public leveling commands extend (not break) the by-construction contract ---

def test_public_commands_unguarded_and_disjoint_from_admin(loop):
    # /rank·/leaderboard 는 멤버 공개 비가드 명령. admin 명령과 분리(disjoint)되고
    # 전체 등록 = admin ∪ public, admin 은 전부 guarded, public 은 전부 unguarded.
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123, admin_role_id=_ADMIN_ROLE_ID)
    a.register_admin_commands(GuildAdminService(a, a.pending))

    class _LevelingSvcStub:
        def rank_embed(self, *args, **kw):
            return None

        def leaderboard_embed(self, *args, **kw):
            return None

    a.register_leveling_commands(_LevelingSvcStub())

    all_names = {c.name for c in a._client.pending_application_commands}
    assert all_names == set(a._admin_commands) | set(a._public_commands)
    assert set(a._public_commands) == {"rank", "leaderboard"}
    assert not (set(a._admin_commands) & set(a._public_commands))  # disjoint
    for c in a._client.pending_application_commands:
        guarded = getattr(c.callback, "__gjc_admin_guarded__", False)
        if c.name in set(a._public_commands):
            assert not guarded, f"{c.name} public command must NOT be admin-guarded"
        else:
            assert guarded, f"{c.name} admin command must be guarded by construction"
