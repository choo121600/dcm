"""tests/test_leveling_g003_adversarial.py — Adversarial / boundary cases for G003 trust-gating.

Covers (non-overlapping with test_leveling_e2e.py):
  1. quota_check boundary: at-limit blocked; limit-1 allowed remaining-1; level transitions
     (lvl4→5 and lvl9→10 crossing XP thresholds) raise the daily limit and unblock a user.
  2. quota_check fail-open: get_record or get_daily_usage raising must yield (True, 0) — no block.
  3. record_usage counter isolation: web/llm cells are independent; different (guild,user) cells
     are not affected by increments to another.
  4. Orchestrator gating via AsyncMock llm.complete:
       (a) over web limit → complete called with web_search=False, reply still returned (no silence)
       (b) under limit + web_used=True → web_search=True, daily_usage(web)==1
       (c) web_used=False → daily_usage unchanged at 0
       (d) guild_id falsy (0) → gating skipped; no crash; web_search follows cooldown only (True)
       (e) g1 over-limit does not degrade g2
  5. scheduler _prune_daily_usage: rows with utc_day < today are deleted; today's row survives.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dcm.leveling.scoring import cum_cost, utc_day
from dcm.leveling.service import LevelingService
from dcm.leveling.store import LevelingStore
from dcm.orchestrator import Orchestrator
from dcm.platform.base import IncomingMessage
from dcm.scheduler import BackgroundJobs


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _incoming(guild_id=1, author_id: str = "u1", text: str = "검색해줘"):
    return IncomingMessage(
        channel_id="c1",
        author_id=author_id,
        author_name="춘식",
        content=text,
        guild_id=guild_id,
    )


def _persona(tmp: str) -> Path:
    p = Path(tmp) / "persona.md"
    p.write_text("너는 지우. 친근하게 반말로 대답해.", encoding="utf-8")
    return p


def _store(tmp: str) -> LevelingStore:
    return LevelingStore(os.path.join(tmp, "leveling.db"))


def _service(store: LevelingStore) -> LevelingService:
    return LevelingService(store)


def _llm(reply: str = "응답이야", web_used: bool = False) -> MagicMock:
    m = MagicMock()
    m.complete = AsyncMock(return_value=(reply, web_used))
    return m


def _orch(tmp: str, leveling, llm) -> Orchestrator:
    return Orchestrator(
        llm=llm,
        persona_path=_persona(tmp),
        bot_name="지우",
        max_input_chars=4000,
        leveling=leveling,
    )


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. quota_check boundary
# ---------------------------------------------------------------------------

class TestQuotaCheckBoundary:
    """Exact boundary arithmetic for WEB_QUOTA_TIERS level-0 limit=20."""

    def test_usage_exactly_at_limit_is_blocked(self):
        """used == limit → allowed=False, remaining=0."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            svc = _service(store)
            try:
                day = utc_day(time.time())
                for _ in range(20):  # exhaust lvl-0 web limit (20)
                    store.incr_daily_usage("g1", "u1", day, "web")
                allowed, remaining = svc.quota_check("g1", "u1", "web", day=day)
                assert allowed is False
                assert remaining == 0
            finally:
                store.close()

    def test_usage_one_below_limit_is_allowed_remaining_1(self):
        """used == limit-1 → allowed=True, remaining=1."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            svc = _service(store)
            try:
                day = utc_day(time.time())
                for _ in range(19):  # limit-1 uses
                    store.incr_daily_usage("g1", "u1", day, "web")
                allowed, remaining = svc.quota_check("g1", "u1", "web", day=day)
                assert allowed is True
                assert remaining == 1
            finally:
                store.close()

    def test_level5_transition_raises_limit_and_unblocks(self):
        """User blocked at lvl-4 web limit=20; adding XP to reach lvl-5 (limit=50) unblocks them.

        cum_cost(5) = sum(level_step(0..4)) = 100+155+220+295+380 = 1150 XP.
        At 1149 XP user is level 4; at 1150 XP user is level 5.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            svc = _service(store)
            try:
                day = utc_day(time.time())
                xp_lvl4 = cum_cost(5) - 1  # 1149 → level 4, limit=20
                assert xp_lvl4 == 1149
                store.add_xp("g1", "u1", xp_lvl4, now=time.time())
                # Exhaust level-4 limit
                for _ in range(20):
                    store.incr_daily_usage("g1", "u1", day, "web")
                # Must be blocked
                allowed, _ = svc.quota_check("g1", "u1", "web", day=day)
                assert allowed is False, "level-4 user with 20 uses must be blocked"
                # Promote: +1 XP → total 1150 = cum_cost(5) → level 5, limit=50
                store.add_xp("g1", "u1", 1, now=time.time())
                allowed, remaining = svc.quota_check("g1", "u1", "web", day=day)
                assert allowed is True, "level-5 limit=50, used=20 → should be allowed"
                assert remaining == 30
            finally:
                store.close()

    def test_level10_transition_raises_limit_and_unblocks(self):
        """User blocked at lvl-9 web limit=50; reaching lvl-10 (limit=100) unblocks them.

        cum_cost(10) = 4675 XP.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            svc = _service(store)
            try:
                day = utc_day(time.time())
                xp_lvl9 = cum_cost(10) - 1  # 4674 → level 9, limit=50
                assert xp_lvl9 == 4674
                store.add_xp("g1", "u1", xp_lvl9, now=time.time())
                for _ in range(50):  # exhaust lvl-9 limit (tier ≥5 = 50)
                    store.incr_daily_usage("g1", "u1", day, "web")
                allowed, _ = svc.quota_check("g1", "u1", "web", day=day)
                assert allowed is False, "level-9 user with 50 uses must be blocked"
                # Promote to level 10 (limit=100)
                store.add_xp("g1", "u1", 1, now=time.time())
                allowed, remaining = svc.quota_check("g1", "u1", "web", day=day)
                assert allowed is True, "level-10 limit=100, used=50 → should be allowed"
                assert remaining == 50
            finally:
                store.close()


# ---------------------------------------------------------------------------
# 2. quota_check fail-open (store errors must never block the bot)
# ---------------------------------------------------------------------------

class _AlwaysRaisingStore:
    """Stub store that raises on every call to simulate a broken DB."""

    def get_record(self, *a, **kw):
        raise RuntimeError("DB unavailable")

    def get_daily_usage(self, *a, **kw):
        raise RuntimeError("DB unavailable")

    def incr_daily_usage(self, *a, **kw):
        raise RuntimeError("DB unavailable")


class _OkRecordRaisingUsageStore:
    """Stub: get_record succeeds; get_daily_usage always raises."""

    def get_record(self, *a, **kw):
        return (0, 0.0)

    def get_daily_usage(self, *a, **kw):
        raise RuntimeError("daily_usage table unavailable")


class TestQuotaCheckFailOpen:
    def test_get_record_raises_returns_true_zero(self):
        svc = LevelingService(_AlwaysRaisingStore())
        allowed, remaining = svc.quota_check("g", "u", "web")
        assert allowed is True
        assert remaining == 0

    def test_get_daily_usage_raises_returns_true_zero(self):
        svc = LevelingService(_OkRecordRaisingUsageStore())
        allowed, remaining = svc.quota_check("g", "u", "web")
        assert allowed is True
        assert remaining == 0

    def test_fail_open_never_raises(self):
        """quota_check must not propagate any exception regardless of store state."""
        svc = LevelingService(_AlwaysRaisingStore())
        # Must not raise
        result = svc.quota_check("g", "u", "llm")
        assert isinstance(result, tuple) and len(result) == 2
        assert result[0] is True

    def test_fail_open_logs_not_silently_ignored(self, caplog):
        """quota_check failure should be logged (leveling quota_check failed)."""
        import logging
        svc = LevelingService(_AlwaysRaisingStore())
        with caplog.at_level(logging.ERROR, logger="dcm.leveling.service"):
            svc.quota_check("g", "u", "web")
        assert any("quota_check" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 3. record_usage counter independence + guild isolation
# ---------------------------------------------------------------------------

class TestRecordUsageIsolation:
    def test_web_and_llm_counters_are_independent(self):
        """web and llm daily counters for the same (guild, user) are separate cells."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            svc = _service(store)
            try:
                day = utc_day(time.time())
                for _ in range(5):
                    svc.record_usage("g", "u", "web", day=day)
                for _ in range(3):
                    svc.record_usage("g", "u", "llm", day=day)
                assert store.get_daily_usage("g", "u", day, "web") == 5
                assert store.get_daily_usage("g", "u", day, "llm") == 3
            finally:
                store.close()

    def test_increment_does_not_spill_to_other_user(self):
        """Incrementing (g1, u1, web) must not change (g1, u2, web)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            svc = _service(store)
            try:
                day = utc_day(time.time())
                for _ in range(7):
                    svc.record_usage("g1", "u1", "web", day=day)
                assert store.get_daily_usage("g1", "u1", day, "web") == 7
                assert store.get_daily_usage("g1", "u2", day, "web") == 0
            finally:
                store.close()

    def test_guild_isolation_in_record_usage(self):
        """Incrementing guild_a counter must not affect guild_b counter for the same user."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            svc = _service(store)
            try:
                day = utc_day(time.time())
                svc.record_usage("guild_a", "u1", "web", day=day)
                svc.record_usage("guild_a", "u1", "web", day=day)
                svc.record_usage("guild_b", "u1", "web", day=day)
                assert store.get_daily_usage("guild_a", "u1", day, "web") == 2
                assert store.get_daily_usage("guild_b", "u1", day, "web") == 1
            finally:
                store.close()

    def test_record_usage_is_idempotent_increments(self):
        """Each call to record_usage increments by exactly 1."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            svc = _service(store)
            try:
                day = utc_day(time.time())
                for i in range(1, 6):
                    svc.record_usage("g", "u", "web", day=day)
                    assert store.get_daily_usage("g", "u", day, "web") == i
            finally:
                store.close()


# ---------------------------------------------------------------------------
# 4. Orchestrator gating via AsyncMock
# ---------------------------------------------------------------------------

class TestOrchestratorGating:
    def test_a_over_limit_web_false_reply_returned(self):
        """(a) Over web limit: complete called with web_search=False; reply still returned (no silence)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            svc = _service(store)
            try:
                day = utc_day(time.time())
                for _ in range(20):  # exhaust lvl-0 web limit
                    store.incr_daily_usage("1", "u1", day, "web")
                llm = _llm(reply="여기 답이야", web_used=False)
                orch = _orch(tmp, svc, llm)
                reply = run(orch.handle(_incoming(guild_id=1, author_id="u1"), []))
                assert reply == "여기 답이야"  # reply returned, not silenced
                assert llm.complete.await_args.kwargs["web_search"] is False
            finally:
                store.close()

    def test_b_under_limit_web_used_true_search_true_and_usage_1(self):
        """(b) Under limit + LLM uses web: web_search=True sent, daily_usage(web)==1."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            svc = _service(store)
            try:
                llm = _llm(reply="검색결과야", web_used=True)
                orch = _orch(tmp, svc, llm)
                run(orch.handle(_incoming(guild_id=1, author_id="u1"), []))
                assert llm.complete.await_args.kwargs["web_search"] is True
                day = utc_day(time.time())
                assert store.get_daily_usage("1", "u1", day, "web") == 1
            finally:
                store.close()

    def test_c_web_used_false_daily_usage_stays_zero(self):
        """(c) LLM does not use web: daily usage counter stays at 0."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            svc = _service(store)
            try:
                llm = _llm(reply="그냥 답이야", web_used=False)
                orch = _orch(tmp, svc, llm)
                run(orch.handle(_incoming(guild_id=1, author_id="u1"), []))
                day = utc_day(time.time())
                assert store.get_daily_usage("1", "u1", day, "web") == 0
            finally:
                store.close()

    def test_d_falsy_guild_id_skips_gating_no_crash(self):
        """(d) guild_id=0 (falsy): quota_check never called; web_search=True (first call, no cooldown)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            # Even if there were 20 uses under guild_id "0", gating is skipped for falsy id
            day = utc_day(time.time())
            for _ in range(20):
                store.incr_daily_usage("0", "u1", day, "web")
            svc = _service(store)
            try:
                llm = _llm(reply="응답", web_used=False)
                orch = _orch(tmp, svc, llm)
                reply = run(orch.handle(_incoming(guild_id=0, author_id="u1"), []))
                assert reply == "응답"  # no crash, reply returned
                # gating skipped → cooldown-only → first call uses web
                assert llm.complete.await_args.kwargs["web_search"] is True
            finally:
                store.close()

    def test_e_guild_isolation_g1_overlimit_does_not_degrade_g2(self):
        """(e) g1 user exhausts web quota; same user in g2 is still allowed full web."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            svc = _service(store)
            try:
                day = utc_day(time.time())
                for _ in range(20):  # exhaust g1 (guild_id="1") limit
                    store.incr_daily_usage("1", "u1", day, "web")
                llm = _llm(reply="g2 answer", web_used=True)
                orch = _orch(tmp, svc, llm)
                run(orch.handle(_incoming(guild_id=2, author_id="u1"), []))
                assert llm.complete.await_args.kwargs["web_search"] is True  # g2 not degraded
                assert store.get_daily_usage("2", "u1", day, "web") == 1
            finally:
                store.close()


# ---------------------------------------------------------------------------
# 5. Scheduler _prune_daily_usage
# ---------------------------------------------------------------------------

class TestSchedulerPruneDailyUsage:
    def test_prune_deletes_stale_rows_and_keeps_today(self):
        """_prune_daily_usage deletes rows with utc_day < today; today's row is preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            try:
                today = utc_day(time.time())
                yesterday = utc_day(time.time() - 86_400)
                two_days_ago = utc_day(time.time() - 2 * 86_400)

                # Insert one row per date
                store.incr_daily_usage("g", "u", today, "web")
                store.incr_daily_usage("g", "u", yesterday, "web")
                store.incr_daily_usage("g", "u", two_days_ago, "web")

                # Build BackgroundJobs with mocked non-leveling deps, real leveling_store
                bg = BackgroundJobs(
                    store=MagicMock(),
                    llm=MagicMock(),
                    embedder=MagicMock(),
                    prune_interval_hours=24,
                    reflect_interval_hours=24,
                    retention_threshold=0.5,
                    max_delete_ratio=0.2,
                    half_life_base_days=30,
                    forget_mode="soft",
                    reflect_min_episodics=5,
                    ingest_model="claude-3-5-haiku-20241022",
                    leveling_store=store,
                )

                # Call the coroutine directly without starting the full background loop
                run(bg._prune_daily_usage())

                # Today must survive
                assert store.get_daily_usage("g", "u", today, "web") == 1
                # Stale rows must be gone
                assert store.get_daily_usage("g", "u", yesterday, "web") == 0
                assert store.get_daily_usage("g", "u", two_days_ago, "web") == 0
            finally:
                store.close()

    def test_prune_only_deletes_past_keeps_future(self):
        """Rows for future dates (shouldn't exist in practice but must not be deleted)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            try:
                today = utc_day(time.time())
                tomorrow = utc_day(time.time() + 86_400)

                store.incr_daily_usage("g", "u", today, "web")
                store.incr_daily_usage("g", "u", tomorrow, "web")

                bg = BackgroundJobs(
                    store=MagicMock(),
                    llm=MagicMock(),
                    embedder=MagicMock(),
                    prune_interval_hours=24,
                    reflect_interval_hours=24,
                    retention_threshold=0.5,
                    max_delete_ratio=0.2,
                    half_life_base_days=30,
                    forget_mode="soft",
                    reflect_min_episodics=5,
                    ingest_model="claude-3-5-haiku-20241022",
                    leveling_store=store,
                )
                run(bg._prune_daily_usage())

                assert store.get_daily_usage("g", "u", today, "web") == 1
                assert store.get_daily_usage("g", "u", tomorrow, "web") == 1
            finally:
                store.close()
