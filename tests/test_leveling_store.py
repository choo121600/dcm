"""LevelingStore 테스트 (길드격리·일일사용량·leaderboard·동시성·재시작 시드)."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dcm.leveling.store import LevelingStore


def _db(tmp: str) -> str:
    return os.path.join(tmp, "leveling.db")


def test_add_xp_accumulates_and_is_guild_isolated():
    with tempfile.TemporaryDirectory() as tmp:
        s = LevelingStore(_db(tmp))
        try:
            s.add_xp("g1", "u1", 10, now=1.0)
            s.add_xp("g1", "u1", 5, now=2.0)
            s.add_xp("g2", "u1", 100, now=3.0)  # 다른 길드
            xp_g1, last = s.get_record("g1", "u1")
            assert xp_g1 == 15  # 누적
            assert last == 2.0  # 최신 last_award_at
            xp_g2, _ = s.get_record("g2", "u1")
            assert xp_g2 == 100  # 격리
            # 미존재
            assert s.get_record("g1", "nobody") == (0, 0.0)
        finally:
            s.close()


def test_weighted_xp_non_decreasing():
    with tempfile.TemporaryDirectory() as tmp:
        s = LevelingStore(_db(tmp))
        try:
            prev = 0
            for i in range(1, 20):
                s.add_xp("g", "u", 3, now=float(i))
                xp, _ = s.get_record("g", "u")
                assert xp >= prev
                prev = xp
            assert prev == 3 * 19
        finally:
            s.close()


def test_daily_usage_utc_day_scoped_and_prune():
    with tempfile.TemporaryDirectory() as tmp:
        s = LevelingStore(_db(tmp))
        try:
            s.incr_daily_usage("g", "u", "2026-01-01", "web")
            s.incr_daily_usage("g", "u", "2026-01-01", "web")
            s.incr_daily_usage("g", "u", "2026-01-01", "llm")
            assert s.get_daily_usage("g", "u", "2026-01-01", "web") == 2
            assert s.get_daily_usage("g", "u", "2026-01-01", "llm") == 1
            # 다른 날 = 0 (utc_day 한정)
            assert s.get_daily_usage("g", "u", "2026-01-02", "web") == 0
            # 다른 길드 격리
            assert s.get_daily_usage("g2", "u", "2026-01-01", "web") == 0
            # prune: 2026-01-02 미만 삭제 → 01-01 사라짐
            s.incr_daily_usage("g", "u", "2026-01-02", "web")
            s.prune_daily_usage("2026-01-02")
            assert s.get_daily_usage("g", "u", "2026-01-01", "web") == 0
            assert s.get_daily_usage("g", "u", "2026-01-02", "web") == 1
        finally:
            s.close()


def test_leaderboard_order_and_topn():
    with tempfile.TemporaryDirectory() as tmp:
        s = LevelingStore(_db(tmp))
        try:
            s.add_xp("g", "low", 10, now=1.0)
            s.add_xp("g", "high", 100, now=1.0)
            s.add_xp("g", "mid", 50, now=1.0)
            s.add_xp("other", "x", 999, now=1.0)  # 다른 길드 제외
            board = s.leaderboard("g", top_n=2)
            assert board == [("high", 100), ("mid", 50)]
            full = s.leaderboard("g", top_n=10)
            assert [u for u, _ in full] == ["high", "mid", "low"]
        finally:
            s.close()


def test_concurrent_enqueue_no_exception_and_consistent():
    with tempfile.TemporaryDirectory() as tmp:
        s = LevelingStore(_db(tmp))
        try:
            def worker():
                for _ in range(50):
                    s.add_xp("g", "u", 1, now=1.0)

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            # 읽기는 같은 큐 FIFO 로 직렬화 → 모든 쓰기 반영
            xp, _ = s.get_record("g", "u")
            assert xp == 4 * 50
        finally:
            s.close()


def test_restart_persists_xp_and_last_award_seed():
    with tempfile.TemporaryDirectory() as tmp:
        path = _db(tmp)
        s1 = LevelingStore(path)
        s1.add_xp("g", "u", 42, now=123.5)
        s1.close()  # drain + close
        # 재시작 — last_award_at 시드 복구 가능해야 함
        s2 = LevelingStore(path)
        try:
            xp, last = s2.get_record("g", "u")
            assert xp == 42
            assert last == 123.5
        finally:
            s2.close()


def test_role_rewards_crud():
    with tempfile.TemporaryDirectory() as tmp:
        s = LevelingStore(_db(tmp))
        try:
            s.set_role_reward("g", 5, 111)
            s.set_role_reward("g", 10, 222)
            s.set_role_reward("g", 5, 333)  # upsert 동일 레벨 → 교체
            assert s.get_role_rewards("g") == [(5, 333), (10, 222)]
            s.remove_role_reward("g", 5)
            assert s.get_role_rewards("g") == [(10, 222)]
            assert s.get_role_rewards("other") == []
        finally:
            s.close()
