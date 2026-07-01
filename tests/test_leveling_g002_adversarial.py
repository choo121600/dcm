"""G002 adversarial / red-team 테스트.

기존 test_leveling_commands.py 와 중복 없이 경계·실패·불변식 케이스만 다룬다.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from dcm.leveling.scoring import xp_award
from dcm.leveling.service import LevelingService
from dcm.leveling.store import LevelingStore
from dcm.platform.pycord_adapter import PycordAdapter
from dcm.service.guild_settings import GuildSettingsStore

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _event_loop():
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    asyncio.set_event_loop(None)
    lp.close()


def _service(tmp_dir, settings=None, *, cooldown=60.0):
    store = LevelingStore(os.path.join(tmp_dir, "leveling.db"))
    svc = LevelingService(store, settings, default_cooldown=cooldown)
    return svc, store


_NORMAL_MSG = "오늘 회의 자료 정리해서 공유드립니다 확인 부탁드려요"


# ===========================================================================
# 1. record_message — 쓰레기 입력값
# ===========================================================================


def test_record_message_none_text_returns_false_no_exception():
    """None 텍스트 → 예외 없이 False 반환 (xp_award 내부서 None or '' 처리)."""
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            result = svc.record_message("g", "u", None, monotonic_time=1.0)  # type: ignore[arg-type]
            assert result is False
        finally:
            store.close()


def test_record_message_empty_string_returns_false():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            assert svc.record_message("g", "u", "", monotonic_time=1.0) is False
        finally:
            store.close()


def test_record_message_whitespace_only_returns_false():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            assert svc.record_message("g", "u", "   \t\n  ", monotonic_time=1.0) is False
        finally:
            store.close()


def test_record_message_spam_awards_reduced_xp():
    """스팸(ㅋㅋㅋㅋㅋ) 은 W_SPAM=0.2 → round(15*0.2)=3 XP 적립."""
    spam = "ㅋ" * 20  # 단일문자 지배 → 도배 판정
    expected_xp = xp_award(spam)
    assert expected_xp == 3  # 계약: W_SPAM(0.2) * BASE_XP(15) = 3

    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            assert svc.record_message("g", "u", spam, monotonic_time=1.0) is True
            xp, _ = store.get_record("g", "u")
            # writer is async; flush via a second get_record (wait=True serializes after write)
            # retry up to 200 ms
            import time
            deadline = time.monotonic() + 0.2
            while xp == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
                xp, _ = store.get_record("g", "u")
            assert xp == expected_xp, f"expected {expected_xp} XP for spam, got {xp}"
        finally:
            store.close()


def test_record_message_very_long_text_awards_normal_xp():
    """초장문(10 000자)은 W_NORMAL → 15 XP 적립되고 예외 없음."""
    long_text = "안녕하세요 " * 1_000  # 6000 chars
    expected = xp_award(long_text)
    assert expected == 15  # W_NORMAL

    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            result = svc.record_message("g", "u", long_text, monotonic_time=1.0)
            assert result is True
        finally:
            store.close()


# ===========================================================================
# 2. record_message — settings.get() 예외 → degrade(기본값 유지, 적립 지속)
# ===========================================================================


def test_record_message_settings_explodes_degrades_to_defaults():
    """settings.get() 가 예외를 던져도 기본값(활성/60s쿨다운)으로 degrade, 적립은 계속된다."""

    class _ExplodingSettings:
        def get(self, gid):
            raise RuntimeError("DB unreachable")

    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _ExplodingSettings())
        try:
            # 활성 degrade → 정상 적립 가능
            assert svc.record_message("g", "u", _NORMAL_MSG, monotonic_time=1.0) is True
            # 60s 기본 쿨다운: 30s 후 → 차단
            assert svc.record_message("g", "u", _NORMAL_MSG, monotonic_time=31.0) is False
            # 60s 초과 → 재적립
            assert svc.record_message("g", "u", _NORMAL_MSG, monotonic_time=62.0) is True
        finally:
            store.close()


# ===========================================================================
# 3. record_message — store.add_xp 예외 → 침묵 False 반환
# ===========================================================================


class _ExplodingStore:
    """store.add_xp 에서 즉시 예외를 던지는 페이크 스토어."""

    def add_xp(self, *_a, **_kw):
        raise OSError("disk full")

    def get_record(self, *_a, **_kw):
        return (0, 0.0)


def test_record_message_store_add_xp_exception_silent_false():
    """store.add_xp 가 예외를 던지면 record_message 는 False 를 반환하고 예외를 전파하지 않는다."""
    svc = LevelingService(_ExplodingStore())
    result = svc.record_message("g", "u", _NORMAL_MSG, monotonic_time=1.0)
    assert result is False


# ===========================================================================
# 4. 쿨다운 불변식 — 연속 호출 순서
# ===========================================================================


def test_cooldown_invariant_monotonic_sequence():
    """단조 증가 시간 시퀀스에서 쿨다운 경계를 정확히 검사한다."""
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, cooldown=10.0)
        try:
            # t=0: 최초 적립
            assert svc.record_message("g", "u", _NORMAL_MSG, monotonic_time=0.0) is True
            # t=9: 쿨다운 미경과 (9 < 10)
            assert svc.record_message("g", "u", _NORMAL_MSG, monotonic_time=9.0) is False
            # t=10: 경계값 — 10 - 0 < 10 은 False → 쿨다운 만료, 적립됨
            assert svc.record_message("g", "u", _NORMAL_MSG, monotonic_time=10.0) is True
            # 재적립 기준이 10.0 으로 갱신됨 → t=10.001: 0.001 < 10 → 차단
            assert svc.record_message("g", "u", _NORMAL_MSG, monotonic_time=10.001) is False
            # t=20.1: 10.0 기준에서 10.1s 경과 → 적립
            assert svc.record_message("g", "u", _NORMAL_MSG, monotonic_time=20.1) is True
        finally:
            store.close()


def test_cooldown_empty_message_does_not_advance_timer():
    """빈/무가치 메시지는 쿨다운 타이머를 갱신하지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, cooldown=10.0)
        try:
            # t=0: 적립
            assert svc.record_message("g", "u", _NORMAL_MSG, monotonic_time=0.0) is True
            # t=5: 빈 메시지 (타이머 갱신 안 됨)
            assert svc.record_message("g", "u", "", monotonic_time=5.0) is False
            # t=5: None (타이머 갱신 안 됨)
            assert svc.record_message("g", "u", None, monotonic_time=5.0) is False  # type: ignore[arg-type]
            # t=10.1: 원래 기준(0.0)에서 10.1s 경과 → 적립돼야 함
            assert svc.record_message("g", "u", _NORMAL_MSG, monotonic_time=10.1) is True
        finally:
            store.close()


def test_cooldown_multiple_users_independent():
    """같은 길드에서 다른 유저의 쿨다운은 서로 독립적이다."""
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, cooldown=30.0)
        try:
            assert svc.record_message("g", "u1", _NORMAL_MSG, monotonic_time=1.0) is True
            assert svc.record_message("g", "u2", _NORMAL_MSG, monotonic_time=1.0) is True
            # u1 은 쿨다운, u2 도 별도로 쿨다운
            assert svc.record_message("g", "u1", _NORMAL_MSG, monotonic_time=15.0) is False
            assert svc.record_message("g", "u2", _NORMAL_MSG, monotonic_time=15.0) is False
        finally:
            store.close()


# ===========================================================================
# 5. guild_settings _migrate — 구 스키마 처리
# ===========================================================================


def _create_old_schema_db(db_path: str) -> None:
    """leveling 컬럼 없는 구버전 guild_settings 테이블 생성."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE guild_settings (
            guild_id           TEXT PRIMARY KEY,
            admin_role_id      INTEGER,
            welcome_channel_id INTEGER,
            default_role_id    INTEGER,
            welcome_message    TEXT,
            updated_at         REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO guild_settings (guild_id, admin_role_id, updated_at) VALUES ('g1', 42, 0.0)"
    )
    conn.commit()
    conn.close()


def test_migrate_adds_leveling_columns_to_old_schema():
    """구 스키마 DB 열면 _migrate 가 leveling 3컬럼을 ALTER TABLE 로 추가한다."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "settings.db")
        _create_old_schema_db(db_path)

        store = GuildSettingsStore(db_path)
        try:
            # 컬럼 존재 확인
            conn = sqlite3.connect(db_path)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(guild_settings)").fetchall()}
            conn.close()
            assert "leveling_enabled" in cols
            assert "leveling_cooldown_seconds" in cols
            assert "leveling_top_n" in cols

            # 기존 레코드는 보존
            s = store.get("g1")
            assert s.admin_role_id == 42
        finally:
            store.close()


def test_migrate_set_get_leveling_fields_after_migration():
    """마이그레이션 후 set_leveling_* / get 이 정상 동작한다."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "settings.db")
        _create_old_schema_db(db_path)

        store = GuildSettingsStore(db_path)
        try:
            store.set_leveling_enabled("g1", True)
            store.set_leveling_cooldown_seconds("g1", 30.0)
            store.set_leveling_top_n("g1", 5)

            s = store.get("g1")
            assert s.leveling_enabled is True
            assert s.leveling_cooldown_seconds == 30.0
            assert s.leveling_top_n == 5
        finally:
            store.close()


def test_migrate_idempotent_double_open():
    """두 번 열어도 오류 없고 데이터 보존(idempotent)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "settings.db")
        _create_old_schema_db(db_path)

        # 1차 오픈 + 데이터 입력
        store1 = GuildSettingsStore(db_path)
        store1.set_leveling_enabled("g1", False)
        store1.close()

        # 2차 오픈 — ALTER 가 이미 존재하는 컬럼에 실행돼도 오류 없음
        store2 = GuildSettingsStore(db_path)
        try:
            s = store2.get("g1")
            assert s.leveling_enabled is False
        finally:
            store2.close()


def test_migrate_unknown_guild_returns_defaults_after_migration():
    """마이그레이션 후 신규 길드 조회 시 leveling 필드가 None(=서비스 기본) 으로 반환된다."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "settings.db")
        _create_old_schema_db(db_path)

        store = GuildSettingsStore(db_path)
        try:
            s = store.get("newguild")
            assert s.leveling_enabled is None
            assert s.leveling_cooldown_seconds is None
            assert s.leveling_top_n is None
        finally:
            store.close()


# ===========================================================================
# 6. 명령 콜백 — rank/leaderboard 엣지케이스 (예외 없이 응답, 비-ephemeral)
# ===========================================================================


def _get_cmd(adapter: PycordAdapter, name: str):
    return next(c for c in adapter._client.pending_application_commands if c.name == name)


class _BaseCtx:
    """최소 discord ctx 시뮬레이터."""

    def __init__(self, guild_id=123, guild=None):
        self.author = types.SimpleNamespace(id=99, display_name="테스터")
        self.guild_id = guild_id
        self.guild = guild
        self.sent: list[dict] = []

    async def respond(self, *_args, **kw):
        self.sent.append(kw)


def test_rank_command_guild_none_no_exception(_event_loop):
    """ctx.guild=None 이어도 rank 명령 정상 응답, 비-ephemeral."""
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            a = PycordAdapter(token="x", bot_name="봇", guild_id=123, admin_role_id=0)
            a.register_leveling_commands(svc)
            rank_cmd = _get_cmd(a, "rank")

            ctx = _BaseCtx(guild_id=123, guild=None)
            _event_loop.run_until_complete(rank_cmd.callback(ctx))

            assert ctx.sent, "응답이 없음"
            assert not ctx.sent[-1].get("ephemeral", False), "ephemeral 으로 전송됨"
        finally:
            store.close()


def test_leaderboard_command_guild_none_no_exception(_event_loop):
    """ctx.guild=None 이어도 leaderboard 명령 정상 응답, 비-ephemeral."""
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            a = PycordAdapter(token="x", bot_name="봇", guild_id=123, admin_role_id=0)
            a.register_leveling_commands(svc)
            lb_cmd = _get_cmd(a, "leaderboard")

            ctx = _BaseCtx(guild_id=123, guild=None)
            _event_loop.run_until_complete(lb_cmd.callback(ctx))

            assert ctx.sent, "응답이 없음"
            assert not ctx.sent[-1].get("ephemeral", False)
        finally:
            store.close()


def test_leaderboard_command_guild_no_get_member_no_exception(_event_loop):
    """guild 객체에 get_member 속성이 없어도 leaderboard 정상 응답."""
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.add_xp("123", "u1", 100, now=1.0)
            a = PycordAdapter(token="x", bot_name="봇", guild_id=123, admin_role_id=0)
            a.register_leveling_commands(svc)
            lb_cmd = _get_cmd(a, "leaderboard")

            # get_member 없는 guild
            guild_no_member = types.SimpleNamespace(id=123)  # no get_member
            ctx = _BaseCtx(guild_id=123, guild=guild_no_member)
            _event_loop.run_until_complete(lb_cmd.callback(ctx))

            assert ctx.sent
            assert not ctx.sent[-1].get("ephemeral", False)
        finally:
            store.close()


def test_leaderboard_command_get_member_returns_none_falls_back(_event_loop):
    """guild.get_member 가 None 반환 → <@uid> 폴백, 예외 없이 응답."""
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            store.add_xp("123", "42", 50, now=1.0)
            a = PycordAdapter(token="x", bot_name="봇", guild_id=123, admin_role_id=0)
            a.register_leveling_commands(svc)
            lb_cmd = _get_cmd(a, "leaderboard")

            guild = types.SimpleNamespace(id=123, get_member=lambda _: None)
            ctx = _BaseCtx(guild_id=123, guild=guild)
            _event_loop.run_until_complete(lb_cmd.callback(ctx))

            assert ctx.sent
            embed = ctx.sent[-1]["embed"]
            # 폴백 멘션 형식
            assert "<@42>" in embed.description
        finally:
            store.close()


# ===========================================================================
# 7. leaderboard 다수 멤버(50명) 정렬 불변식 · top_n 준수
# ===========================================================================


def test_leaderboard_50_members_sorted_and_top_n():
    """50명 유저를 역순으로 입력해도 leaderboard 는 XP 내림차순으로 정확히 반환한다."""
    import time as _time

    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            n = 50
            # 역순으로 삽입: user_49=50 XP, user_48=100 XP, …, user_0=2500 XP
            for i in range(n - 1, -1, -1):
                xp = (i + 1) * 50  # user_0→2500, user_49→50
                store.add_xp("g", f"user_{i}", xp, now=float(i))

            # writer 큐가 flush 될 때까지 짧게 대기 (read 는 wait=True 로 직렬화됨)
            deadline = _time.monotonic() + 1.0
            while True:
                rows = store.leaderboard("g", 50)
                if len(rows) == n:
                    break
                if _time.monotonic() > deadline:
                    pytest.fail(f"leaderboard 행 수 부족: {len(rows)}/{n}")
                _time.sleep(0.02)

            # 내림차순 정렬 불변식
            xps = [xp for _, xp in rows]
            assert xps == sorted(xps, reverse=True), "XP 내림차순 정렬 위반"
            assert len(rows) == n

            # top_n=10 제한
            top10 = store.leaderboard("g", 10)
            assert len(top10) == 10
            # user_49 가 최고 XP: xp=(49+1)*50=2500
            assert top10[0] == ("user_49", 2500)
            assert top10[0][0] == "user_49"
            assert top10[0][1] == 2500
        finally:
            store.close()


def test_leaderboard_embed_50_members_desc_sorted(_event_loop):
    """leaderboard_embed 가 50명일 때 embed description 내 순서가 XP 기준 내림차순."""
    import time as _time

    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp)
        try:
            n = 50
            for i in range(n):
                store.add_xp("g", f"u{i}", (i + 1) * 10, now=float(i))

            deadline = _time.monotonic() + 1.0
            while True:
                rows = store.leaderboard("g", n)
                if len(rows) == n:
                    break
                if _time.monotonic() > deadline:
                    pytest.fail("leaderboard 행 수 부족")
                _time.sleep(0.02)

            # top_n_for: 설정 없으면 default=10 → embed 에는 10명만
            embed = svc.leaderboard_embed("g")
            lines = embed.description.strip().split("\n")
            assert len(lines) == 10  # 기본 top_n=10

            # 순위 1번이 가장 높은 XP (u49=500)
            assert "**1.**" in lines[0]
            assert "u49" in lines[0] or "500" in lines[0]

        finally:
            store.close()
