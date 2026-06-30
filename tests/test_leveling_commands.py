"""LevelingService 핫패스/표시 + 공개 슬래시 명령 테스트 (G002)."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from dcm.leveling.service import LevelingService
from dcm.leveling.store import LevelingStore
from dcm.platform.pycord_adapter import PycordAdapter
from dcm.service.guild_settings import GuildSettings


@pytest.fixture(autouse=True)
def loop():
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    asyncio.set_event_loop(None)
    lp.close()


def _service(tmp, settings=None):
    store = LevelingStore(os.path.join(tmp, "leveling.db"))
    return LevelingService(store, settings, default_cooldown=60.0), store


_LONG = "오늘 회의 자료 정리해서 공유드립니다 확인 부탁드려요"


def test_record_message_awards_then_cooldown_blocks_then_awards():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            assert svc.record_message("g", "u", _LONG, monotonic_time=100.0) is True
            # 쿨다운(60s) 내 → 미적립
            assert svc.record_message("g", "u", _LONG, monotonic_time=130.0) is False
            # 쿨다운 경과 → 재적립
            assert svc.record_message("g", "u", _LONG, monotonic_time=200.0) is True
            xp, _ = store.get_record("g", "u")
            assert xp == 30  # 2 * 15
        finally:
            store.close()


def test_empty_message_skipped_without_consuming_cooldown():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            assert svc.record_message("g", "u", "    ", monotonic_time=100.0) is False
            # 빈 메시지는 쿨다운을 소비하지 않으므로 바로 다음 정상 메시지는 적립
            assert svc.record_message("g", "u", _LONG, monotonic_time=100.5) is True
            xp, _ = store.get_record("g", "u")
            assert xp == 15
        finally:
            store.close()


def test_disabled_guild_skips_award():
    class _Settings:
        def get(self, gid):
            return GuildSettings(guild_id=str(gid), leveling_enabled=False)

    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings())
        try:
            assert svc.record_message("g", "u", _LONG, monotonic_time=1.0) is False
            assert store.get_record("g", "u") == (0, 0.0)
        finally:
            store.close()


def test_guild_cooldown_override_respected():
    class _Settings:
        def get(self, gid):
            return GuildSettings(guild_id=str(gid), leveling_cooldown_seconds=5.0)

    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings())
        try:
            assert svc.record_message("g", "u", _LONG, monotonic_time=0.0) is True
            # 5s override: 6s 후엔 적립
            assert svc.record_message("g", "u", _LONG, monotonic_time=6.0) is True
            xp, _ = store.get_record("g", "u")
            assert xp == 30
        finally:
            store.close()


def test_guild_isolation_in_cooldown():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            assert svc.record_message("g1", "u", _LONG, monotonic_time=1.0) is True
            # 다른 길드 같은 user → 독립 쿨다운
            assert svc.record_message("g2", "u", _LONG, monotonic_time=1.0) is True
        finally:
            store.close()


def test_rank_embed_reflects_xp():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            svc.record_message("g", "u", _LONG, monotonic_time=1.0)  # +15
            embed = svc.rank_embed("g", "u", "춘식")
            assert "춘식" in embed.title
            fields = {f.name: f.value for f in embed.fields}
            assert fields["레벨"] == "0"
            assert fields["총 XP"] == "15"
        finally:
            store.close()


def test_leaderboard_embed_orders_and_resolves_names():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.add_xp("g", "alice", 200, now=1.0)
            store.add_xp("g", "bob", 50, now=1.0)
            names = {"alice": "앨리스", "bob": "밥"}
            embed = svc.leaderboard_embed("g", name_resolver=lambda uid: names[uid])
            assert "앨리스" in embed.description and "밥" in embed.description
            assert embed.description.index("앨리스") < embed.description.index("밥")
        finally:
            store.close()


def test_leaderboard_embed_empty_message():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            embed = svc.leaderboard_embed("emptyguild")
            assert "아직" in embed.description
        finally:
            store.close()


def test_register_leveling_commands_public_and_unguarded():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            a = PycordAdapter(token="x", bot_name="지우", guild_id=123, admin_role_id=999)
            a.register_leveling_commands(svc)
            assert set(a._public_commands) == {"rank", "leaderboard"}
            # register_leveling_commands 는 공개 표시 명령(rank/leaderboard) + 관리자 설정
            # 명령(set/remove/list-level-role)을 함께 등록한다.
            assert set(a._admin_commands) == {
                "set-level-role",
                "remove-level-role",
                "list-level-roles",
            }
            names = {c.name for c in a._client.pending_application_commands}
            assert {"rank", "leaderboard"} <= names
            for c in a._client.pending_application_commands:
                guarded = getattr(c.callback, "__gjc_admin_guarded__", False)
                if c.name in ("rank", "leaderboard"):
                    assert not guarded  # 공개 = 비가드
                else:
                    assert guarded  # 레벨→역할 설정 = admin 가드
        finally:
            store.close()


def test_rank_command_responds_with_public_embed(loop):
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            svc.record_message("123", "42", _LONG, monotonic_time=1.0)
            a = PycordAdapter(token="x", bot_name="지우", guild_id=123, admin_role_id=999)
            a.register_leveling_commands(svc)
            rank_cmd = next(
                c for c in a._client.pending_application_commands if c.name == "rank"
            )

            class _Ctx:
                def __init__(self):
                    self.author = types.SimpleNamespace(id=42, display_name="춘식")
                    self.guild_id = 123
                    self.guild = types.SimpleNamespace(id=123, get_member=lambda i: None)
                    self.sent = []

                async def respond(self, *args, **kw):
                    self.sent.append(kw)

            ctx = _Ctx()
            loop.run_until_complete(rank_cmd.callback(ctx))
            assert ctx.sent, "no response"
            # 공개(비-ephemeral): ephemeral 플래그 미설정
            assert not ctx.sent[-1].get("ephemeral", False)
            embed = ctx.sent[-1]["embed"]
            assert "춘식" in embed.title
        finally:
            store.close()


def test_settings_cache_avoids_per_message_db_reads():
    # 핫패스 DB read 0(steady state): TTL(60s) 내 여러 메시지에 settings.get 은 1회만 호출.
    class _CountingSettings:
        def __init__(self):
            self.calls = 0

        def get(self, gid):
            self.calls += 1
            return GuildSettings(guild_id=str(gid), leveling_cooldown_seconds=5.0)

    with tempfile.TemporaryDirectory() as tmp:
        cs = _CountingSettings()
        svc, store = _service(tmp, cs)
        try:
            for t in (0.0, 6.0, 12.0, 18.0):  # 5s 쿨다운 경과마다 적립
                svc.record_message("g", "u", _LONG, monotonic_time=t)
            assert cs.calls == 1  # TTL 내 캐시 히트 → DB read 1회뿐
            # TTL(60s) 경과 → 재조회
            svc.record_message("g", "u", _LONG, monotonic_time=70.0)
            assert cs.calls == 2
            xp, _ = store.get_record("g", "u")
            assert xp == 75  # 5회 적립 * 15
        finally:
            store.close()
