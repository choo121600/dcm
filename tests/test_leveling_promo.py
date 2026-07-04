"""Promotion-verification bonus (홍보 인증 보너스).

Covers the pure proof detector (contains_url), the service award logic (channel scoping,
proof gating, per-UTC-day cap, custom bonus/cap, disabled-leveling short-circuit), settings
persistence + migration, and the `set-promo` / `show-config` admin-command wiring.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dcm.leveling.scoring import PROMO_BONUS_XP, contains_url, utc_day
from dcm.leveling.service import LevelingService, PromoResult
from dcm.leveling.store import LevelingStore
from dcm.platform.pycord_adapter import PycordAdapter
from dcm.service.guild_admin import GuildAdminService
from dcm.service.guild_settings import GuildSettings, GuildSettingsStore

_NOW = 1_700_000_000.0  # fixed epoch → deterministic UTC day


@pytest.fixture(autouse=True)
def loop():
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    lp.close()


class _Settings:
    """Fake GuildSettingsStore whose .get() returns one fixed GuildSettings."""

    def __init__(self, **kw):
        self._gs = GuildSettings(guild_id="1", **kw)

    def get(self, gid):
        return self._gs


def _service(tmp, settings):
    store = LevelingStore(os.path.join(tmp, "leveling.db"))
    return LevelingService(store, settings), store


# ───────────────────────── contains_url (proof signal) ─────────────────────────


def test_contains_url_detects_links_and_invites():
    assert contains_url("인증 https://twitter.com/x/status/1")
    assert contains_url("here http://foo.bar")
    assert contains_url("www.example.com 에 홍보함")
    assert contains_url("우리 서버 discord.gg/abcd 놀러와")
    assert contains_url("https://discord.com/invite/abcd")


def test_contains_url_rejects_plain_text():
    assert not contains_url("홍보 다 했어요 인증합니다")
    assert not contains_url("")
    assert not contains_url("no links here just words")


# ───────────────────────── maybe_award_promo ─────────────────────────


def test_promo_skips_when_channel_unconfigured():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings())  # promo_channel_id None → feature off
        try:
            assert svc.maybe_award_promo(1, 42, 999, has_proof=True, now=_NOW) is PromoResult.SKIP
            assert store.get_record(1, 42)[0] == 0
        finally:
            store.close()


def test_promo_skips_wrong_channel():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(promo_channel_id=100))
        try:
            assert svc.maybe_award_promo(1, 42, 999, has_proof=True, now=_NOW) is PromoResult.SKIP
            assert store.get_record(1, 42)[0] == 0
        finally:
            store.close()


def test_promo_no_proof_awards_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(promo_channel_id=100))
        try:
            assert svc.maybe_award_promo(1, 42, 100, has_proof=False, now=_NOW) is PromoResult.NO_PROOF
            assert store.get_record(1, 42)[0] == 0
        finally:
            store.close()


def test_promo_awards_bonus_with_proof():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(promo_channel_id=100))
        try:
            assert svc.maybe_award_promo(1, 42, 100, has_proof=True, now=_NOW) is PromoResult.AWARDED
            assert store.get_record(1, 42)[0] == PROMO_BONUS_XP
            assert store.get_daily_usage(1, 42, utc_day(_NOW), "promo") == 1
        finally:
            store.close()


def test_promo_daily_cap_blocks_second_same_day():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(promo_channel_id=100))  # default cap = 1
        try:
            assert svc.maybe_award_promo(1, 42, 100, has_proof=True, now=_NOW) is PromoResult.AWARDED
            assert svc.maybe_award_promo(1, 42, 100, has_proof=True, now=_NOW) is PromoResult.CAPPED
            assert store.get_record(1, 42)[0] == PROMO_BONUS_XP  # only one bonus granted
        finally:
            store.close()


def test_promo_cap_resets_on_next_utc_day():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(promo_channel_id=100))
        try:
            assert svc.maybe_award_promo(1, 42, 100, has_proof=True, now=_NOW) is PromoResult.AWARDED
            later = _NOW + 86400 * 2  # two UTC days later
            assert svc.maybe_award_promo(1, 42, 100, has_proof=True, now=later) is PromoResult.AWARDED
            assert store.get_record(1, 42)[0] == PROMO_BONUS_XP * 2
        finally:
            store.close()


def test_promo_custom_bonus_and_cap():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(
            tmp, _Settings(promo_channel_id=100, promo_bonus_xp=250, promo_daily_cap=2)
        )
        try:
            assert svc.maybe_award_promo(1, 42, 100, has_proof=True, now=_NOW) is PromoResult.AWARDED
            assert svc.maybe_award_promo(1, 42, 100, has_proof=True, now=_NOW) is PromoResult.AWARDED
            assert svc.maybe_award_promo(1, 42, 100, has_proof=True, now=_NOW) is PromoResult.CAPPED
            assert store.get_record(1, 42)[0] == 500
        finally:
            store.close()


def test_promo_skips_when_leveling_disabled():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(promo_channel_id=100, leveling_enabled=False))
        try:
            assert svc.maybe_award_promo(1, 42, 100, has_proof=True, now=_NOW) is PromoResult.SKIP
            assert store.get_record(1, 42)[0] == 0
        finally:
            store.close()


def test_promo_channel_match_is_string_normalized():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(promo_channel_id=100))
        try:
            # int settings vs str channel id (Discord snowflakes) still match
            assert svc.maybe_award_promo("1", "42", "100", has_proof=True, now=_NOW) is PromoResult.AWARDED
        finally:
            store.close()


def test_promo_zero_bonus_is_noop():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(promo_channel_id=100, promo_bonus_xp=0))
        try:
            # bonus_xp=0 falls back to the service default (100), so an award still happens
            assert svc.maybe_award_promo(1, 42, 100, has_proof=True, now=_NOW) is PromoResult.AWARDED
            assert store.get_record(1, 42)[0] == PROMO_BONUS_XP
        finally:
            store.close()


# ───────────────────────── settings persistence + migration ─────────────────────────


def test_guild_settings_promo_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        s = GuildSettingsStore(os.path.join(tmp, "s.db"))
        try:
            s.set_promo_channel(7, 555)
            s.set_promo_bonus_xp(7, 200)
            s.set_promo_daily_cap(7, 3)
            got = s.get(7)
            assert got.promo_channel_id == 555
            assert got.promo_bonus_xp == 200
            assert got.promo_daily_cap == 3
        finally:
            s.close()


def test_guild_settings_promo_defaults_none():
    with tempfile.TemporaryDirectory() as tmp:
        s = GuildSettingsStore(os.path.join(tmp, "s.db"))
        try:
            got = s.get(7)
            assert got.promo_channel_id is None
            assert got.promo_bonus_xp is None
            assert got.promo_daily_cap is None
        finally:
            s.close()


def test_guild_settings_migrates_promo_columns_on_pre_promo_db():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "s.db")
        # Pre-promo (pre-leveling) schema: base columns only. _migrate must add promo columns.
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE guild_settings ("
            "guild_id TEXT PRIMARY KEY, admin_role_id INTEGER, welcome_channel_id INTEGER, "
            "default_role_id INTEGER, welcome_message TEXT, updated_at REAL)"
        )
        conn.commit()
        conn.close()
        s = GuildSettingsStore(path)  # runs _migrate
        try:
            s.set_promo_channel(7, 42)
            assert s.get(7).promo_channel_id == 42
        finally:
            s.close()


# ───────────────────────── set-promo / show-config admin wiring ─────────────────────────


class _RecSettings:
    """Recording settings double for adapter command tests."""

    def __init__(self):
        self.calls: list = []
        self._data: dict = {}

    def get(self, gid):
        d = self._data.get(str(gid), {})
        return types.SimpleNamespace(
            admin_role_id=999,  # matches the ctx author's role → passes InvokerCheck
            welcome_channel_id=None,
            default_role_id=None,
            welcome_message=None,
            promo_channel_id=d.get("promo_channel_id"),
            promo_bonus_xp=d.get("promo_bonus_xp"),
            promo_daily_cap=d.get("promo_daily_cap"),
        )

    def set_promo_channel(self, gid, cid):
        self.calls.append(("channel", gid, cid))
        self._data.setdefault(str(gid), {})["promo_channel_id"] = cid

    def set_promo_bonus_xp(self, gid, xp):
        self.calls.append(("bonus", gid, xp))
        self._data.setdefault(str(gid), {})["promo_bonus_xp"] = xp

    def set_promo_daily_cap(self, gid, cap):
        self.calls.append(("cap", gid, cap))
        self._data.setdefault(str(gid), {})["promo_daily_cap"] = cap


class _AdminCtx:
    def __init__(self):
        self.author = types.SimpleNamespace(
            roles=[types.SimpleNamespace(id=999)], id=42, display_name="choo"
        )
        self.guild_id = 123
        self.responses: list = []

    async def respond(self, text, **kw):
        self.responses.append((text, kw))

    async def defer(self, **kw):
        pass


def _admin_adapter(settings):
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123, admin_role_id=0, guild_settings=settings)
    a.register_admin_commands(GuildAdminService(a, a.pending))
    return a


def _find(a, name):
    return next((c for c in a._client.pending_application_commands if c.name == name), None)


def test_set_promo_registered_and_admin_guarded():
    a = _admin_adapter(_RecSettings())
    cmd = _find(a, "set-promo")
    assert cmd is not None, "set-promo command not registered"
    assert getattr(cmd.callback, "__gjc_admin_guarded__", False) is True


def test_set_promo_records_calls_and_confirms(loop):
    st = _RecSettings()
    a = _admin_adapter(st)
    cmd = _find(a, "set-promo")
    ctx = _AdminCtx()
    loop.run_until_complete(cmd.callback(ctx, channel_id="456", bonus_xp=50, daily_cap=3))
    assert ("channel", 123, 456) in st.calls
    assert ("bonus", 123, 50) in st.calls
    assert ("cap", 123, 3) in st.calls
    text, kw = ctx.responses[-1]
    assert "456" in text
    assert kw.get("ephemeral") is True


def test_set_promo_channel_zero_disables(loop):
    st = _RecSettings()
    a = _admin_adapter(st)
    cmd = _find(a, "set-promo")
    ctx = _AdminCtx()
    loop.run_until_complete(cmd.callback(ctx, channel_id="0"))
    assert ("channel", 123, 0) in st.calls
    text, kw = ctx.responses[-1]
    assert "껐" in text  # promo_disabled (ko locale)
    assert kw.get("ephemeral") is True


def test_set_promo_bad_channel_id_errors(loop):
    st = _RecSettings()
    a = _admin_adapter(st)
    cmd = _find(a, "set-promo")
    ctx = _AdminCtx()
    loop.run_until_complete(cmd.callback(ctx, channel_id="not-a-number"))
    assert st.calls == [], "set_promo_* should not be called on a bad channel id"
    text, _ = ctx.responses[-1]
    assert "정수" in text


def test_show_config_includes_promo_lines(loop):
    st = _RecSettings()
    st._data["123"] = {"promo_channel_id": 456, "promo_bonus_xp": 77, "promo_daily_cap": 5}
    a = _admin_adapter(st)
    cmd = _find(a, "show-config")
    ctx = _AdminCtx()
    loop.run_until_complete(cmd.callback(ctx))
    text, kw = ctx.responses[-1]
    assert "홍보 인증 채널" in text
    assert "456" in text and "77" in text and "5" in text
    assert kw.get("ephemeral") is True
