"""적대적 경계·속성 테스트 — scoring 순수함수 + LevelingStore.

기존 test_leveling_scoring.py / test_leveling_store.py 와 중복되지 않는 케이스:
  scoring: 거대 XP 유한성·단조, level/cum_cost/progress 불변식, 음수 방어,
           유니코드·zalgo·탭·zero-width 등 quality_weight 예외 없음,
           xp_award 정수·비음수, utc_day 86399→86400 경계.
  store:   8스레드×200 동시성 정확합·예외 0, 길드 격리 동시성,
           prune 경계(엄밀 < 비교), leaderboard tie-break 결정성,
           동시 쓰기 후 재시작 내구성, role_reward upsert 교체.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dcm.leveling import scoring
from dcm.leveling.store import LevelingStore

# ── scoring: 거대 XP 유한성·단조 ────────────────────────────────────────────────

def test_level_large_xp_finite_and_monotonic():
    """10**9 XP 에서 level() 가 유한·단조 (XP 증가 → 레벨 단조 증가)."""
    prev = -1
    for exp in (6, 7, 8, 9):
        lvl = scoring.level(10 ** exp)
        assert isinstance(lvl, int)
        assert lvl >= 0
        assert lvl > prev, f"level(10^{exp})={lvl} <= level(10^{exp-1})={prev}"
        prev = lvl
    # 10^9 가 루프 없이 유한하게 반환됨
    assert scoring.level(10 ** 9) < 10 ** 9


# ── scoring: level/cum_cost/progress 불변식 ─────────────────────────────────────

def test_level_cum_cost_consistency_property():
    """모든 xp 에서 cum_cost(level(xp)) <= xp < cum_cost(level(xp)+1)."""
    samples = [
        0, 1, 99, 100, 101, 255, 999, 5_000,
        100_000, 1_000_000, 10 ** 9,
    ]
    for xp in samples:
        lvl = scoring.level(xp)
        floor = scoring.cum_cost(lvl)
        ceiling = scoring.cum_cost(lvl + 1)
        assert floor <= xp, f"xp={xp}: floor={floor} > xp"
        assert xp < ceiling, f"xp={xp}: ceiling={ceiling} <= xp"


# ── scoring: 음수 XP 방어 ───────────────────────────────────────────────────────

def test_negative_xp_defense_all_functions():
    """음수 XP → level/progress/xp_to_next 모두 안전한 비음수 결과."""
    for xp in (-1, -100, -10 ** 9):
        assert scoring.level(xp) == 0, f"level({xp}) != 0"
        p = scoring.progress(xp)
        assert 0.0 <= p < 1.0, f"progress({xp})={p} out of [0,1)"
        nxt = scoring.xp_to_next(xp)
        assert nxt > 0, f"xp_to_next({xp})={nxt} <= 0"


# ── scoring: quality_weight 유니코드 엣지케이스 ─────────────────────────────────

def test_quality_weight_unicode_edge_cases_no_exception():
    """유니코드/zalgo/탭/zero-width/제어문자에서 예외 없이 [0,1] 반환."""
    edge_cases = [
        "\t\t\t",                    # 탭만
        "\u200b\u200b\u200b",        # zero-width space
        "Z\u0353\u033da\u0353\u033dl\u0353\u033dg\u0353\u033do\u0353\u033d",  # zalgo
        "\u202e뒤집힌텍스트",           # RTL override
        "a" * 10_000,               # 매우 긴 반복 문자열
        "🎉" * 100,                  # 이모지 많이
        "\n\n\n",                    # 줄바꿈만
        "\u3000" * 5,               # 전각 공백
        "\x00\x01\x02",             # 제어문자
        "\u200c\u200d",             # zero-width non-joiner/joiner
    ]
    for text in edge_cases:
        w = scoring.quality_weight(text)
        assert isinstance(w, float), f"non-float for {text!r}: {type(w)}"
        assert 0.0 <= w <= 1.0, f"out of [0,1] for {text!r}: {w}"


# ── scoring: xp_award 항상 정수·비음수 ─────────────────────────────────────────

def test_xp_award_always_int_and_nonneg():
    """xp_award 는 어떤 입력에서도 항상 int·비음수."""
    inputs = [
        "",
        "    ",
        "ㅋ" * 100,
        "Z\u0353\u033da\u0353\u033dl\u0353\u033dg\u0353\u033do\u0353\u033d",  # zalgo
        "\t",
        "🎉🎉🎉",
        "a" * 10_000,
        "\u200b",
        "오늘 회의 자료 정리해서 공유드립니다",
    ]
    for text in inputs:
        v = scoring.xp_award(text)
        assert isinstance(v, int), f"xp_award not int for {text!r}: {type(v)}"
        assert v >= 0, f"xp_award negative for {text!r}: {v}"


# ── scoring: utc_day 경계 86399 / 86400 ────────────────────────────────────────

def test_utc_day_exact_boundary():
    """86399 → '1970-01-01', 86400 → '1970-01-02' (엄밀 경계)."""
    assert scoring.utc_day(86_399) == "1970-01-01"
    assert scoring.utc_day(86_400) == "1970-01-02"
    assert scoring.utc_day(86_399) != scoring.utc_day(86_400)


# ── store: 8스레드×200 동시 add_xp ─────────────────────────────────────────────

def test_concurrent_add_xp_8_threads_200_each_accurate():
    """8 스레드 × 200 add_xp(1) → 총합=1600, 예외 0."""
    with tempfile.TemporaryDirectory() as tmp:
        s = LevelingStore(os.path.join(tmp, "leveling.db"))
        exceptions: list[Exception] = []
        try:
            def worker():
                try:
                    for i in range(200):
                        s.add_xp("g", "u", 1, now=float(i))
                except Exception as exc:
                    exceptions.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert exceptions == [], f"스레드 예외: {exceptions}"
            xp, _ = s.get_record("g", "u")
            assert xp == 8 * 200, f"xp={xp} expected {8 * 200}"
        finally:
            s.close()


# ── store: 길드 격리 동시성 ─────────────────────────────────────────────────────

def test_guild_isolation_under_concurrent_writes():
    """두 길드 동시 write → 각 길드 XP 독립·정확."""
    with tempfile.TemporaryDirectory() as tmp:
        s = LevelingStore(os.path.join(tmp, "leveling.db"))
        try:
            def writer_a():
                for i in range(100):
                    s.add_xp("guild_a", "u", 3, now=float(i))

            def writer_b():
                for i in range(100):
                    s.add_xp("guild_b", "u", 7, now=float(i))

            ta = threading.Thread(target=writer_a)
            tb = threading.Thread(target=writer_b)
            ta.start()
            tb.start()
            ta.join()
            tb.join()

            xp_a, _ = s.get_record("guild_a", "u")
            xp_b, _ = s.get_record("guild_b", "u")
            assert xp_a == 300, f"guild_a xp={xp_a}"
            assert xp_b == 700, f"guild_b xp={xp_b}"
        finally:
            s.close()


# ── store: prune 경계 엄밀 < ───────────────────────────────────────────────────

def test_prune_boundary_strict_less_than():
    """prune('2026-01-02') → utc_day < '2026-01-02' 만 삭제, 당일·이후는 보존."""
    with tempfile.TemporaryDirectory() as tmp:
        s = LevelingStore(os.path.join(tmp, "leveling.db"))
        try:
            s.incr_daily_usage("g", "u", "2026-01-01", "web")  # 삭제 대상
            s.incr_daily_usage("g", "u", "2026-01-02", "web")  # cutoff 당일 → 보존
            s.incr_daily_usage("g", "u", "2026-01-03", "web")  # 이후 → 보존
            s.prune_daily_usage("2026-01-02")
            # get_daily_usage (wait=True) 가 앞선 큐 항목을 모두 flush 후 읽음
            assert s.get_daily_usage("g", "u", "2026-01-01", "web") == 0, "01-01 미삭제"
            assert s.get_daily_usage("g", "u", "2026-01-02", "web") == 1, "01-02 삭제됨"
            assert s.get_daily_usage("g", "u", "2026-01-03", "web") == 1, "01-03 삭제됨"
        finally:
            s.close()


# ── store: leaderboard tie-break 결정성 ────────────────────────────────────────

def test_leaderboard_tiebreak_deterministic():
    """동점 users → user_id ASC 으로 결정적 정렬 (ORDER BY weighted_xp DESC, user_id ASC)."""
    with tempfile.TemporaryDirectory() as tmp:
        s = LevelingStore(os.path.join(tmp, "leveling.db"))
        try:
            for uid in ("c_user", "a_user", "b_user"):
                s.add_xp("g", uid, 50, now=1.0)
            board = s.leaderboard("g", top_n=10)
            assert [u for u, _ in board] == ["a_user", "b_user", "c_user"], (
                f"tie-break not ASC: {board}"
            )
            assert all(xp == 50 for _, xp in board)
        finally:
            s.close()


# ── store: 동시 쓰기 후 재시작 내구성 ───────────────────────────────────────────

def test_restart_durability_after_concurrent_writes():
    """20스레드 동시 write → close → 재오픈: XP 전량 보존."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "leveling.db")
        s = LevelingStore(path)
        try:
            def _add():
                s.add_xp("g", "u", 1, now=1.0)

            threads = [threading.Thread(target=_add) for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            s.close()  # drain + close

        s2 = LevelingStore(path)
        try:
            xp, _ = s2.get_record("g", "u")
            assert xp == 20, f"재시작 후 xp={xp}"
        finally:
            s2.close()


# ── store: role_reward upsert 교체 ─────────────────────────────────────────────

def test_role_reward_upsert_replaces_no_duplicate():
    """같은 (guild, level) 에 3번 upsert → 마지막 role_id 1개만 존재."""
    with tempfile.TemporaryDirectory() as tmp:
        s = LevelingStore(os.path.join(tmp, "leveling.db"))
        try:
            s.set_role_reward("g", 5, 111)
            s.set_role_reward("g", 5, 222)
            s.set_role_reward("g", 5, 333)
            rewards = s.get_role_rewards("g")
            assert len(rewards) == 1, f"중복 레코드: {rewards}"
            assert rewards[0] == (5, 333), f"최신 role_id 아님: {rewards}"
        finally:
            s.close()
