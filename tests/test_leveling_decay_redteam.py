"""신뢰-하락(Trust Decay) Phase 1 레드팀 — 깨뜨리기 시도.

happy-path(test_leveling_decay.py)와 별개로, 불변식을 무너뜨리려는 적대적 시나리오:
음수 드레인/네거티브-크레딧 게이밍·cap 우회·길드 교차·admin 우회·shadow 우회·핫패스 read.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dcm.leveling import scoring
from dcm.leveling.service import PENALTY_DAILY_CAP, PENALTY_WINDOW_CAP, LevelingService
from dcm.leveling.store import LevelingStore
from dcm.service.guild_settings import GuildSettings

_SPAM = "ㅋㅋㅋㅋㅋ"  # W_SPAM(0.2)=+3, ASCII 알파벳 0
_LONG = "오늘 회의 자료 정리해서 공유드립니다 확인 부탁드려요"


class _Settings:
    def __init__(self, **kw):
        self._gs = GuildSettings(guild_id="g", **kw)

    def get(self, gid):
        return self._gs


def _svc(tmp, **kw):
    store = LevelingStore(os.path.join(tmp, "leveling.db"))
    return LevelingService(store, _Settings(leveling_decay_enabled=True, **kw)), store


def _flood(svc, gid, uid, n, *, base=10.0, step=0.1, now=2.0, **kw):
    for i in range(n):
        svc.record_message(gid, uid, _SPAM, monotonic_time=base + i * step, now=now, **kw)


# ── 음수/드레인 게이밍 ──────────────────────────────────────────────


def test_cannot_drive_below_zero_with_huge_sustained_flood():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp)
        try:
            store.add_xp("g", "u", 40, now=1.0)  # 작은 잔고
            _flood(svc, "g", "u", 200)  # 대량 플러딩
            xp, _ = store.get_record("g", "u")
            assert xp >= 0  # 절대 음수 불가
        finally:
            store.close()


def test_no_negative_credit_banking_recovery_is_full_rate():
    # 바닥(0)까지 깎인 뒤에도 '음수 빚'이 남아 회복을 갉아먹지 않아야 한다.
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp, leveling_cooldown_seconds=0.0)
        try:
            store.add_xp("g", "u", 30, now=1.0)
            _flood(svc, "g", "u", 200)  # 바닥으로
            xp_floor, _ = store.get_record("g", "u")
            assert xp_floor == 0
            # 정상 메시지 1건 → 정확히 +15(숨은 음수 빚 없음)
            assert svc.record_message("g", "u", _LONG, monotonic_time=10_000.0, now=10_000.0) is True
            xp, _ = store.get_record("g", "u")
            assert xp == 15
        finally:
            store.close()


def test_first_message_mention_burst_cannot_seed_negative():
    # 신규 유저의 '첫' 메시지가 멘션 폭주여도 음수로 시드되지 않는다(하한0).
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp)
        try:
            svc.record_message(
                "g", "fresh", "여기 좀 봐", monotonic_time=5.0, now=5.0, mention_count=20
            )
            xp, _ = store.get_record("g", "fresh")
            assert xp == 0  # 음수 아님
        finally:
            store.close()


# ── cap 우회 시도 ───────────────────────────────────────────────────


def test_window_and_daily_caps_bound_total_deduction():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp)
        try:
            store.add_xp("g", "u", 5000, now=1.0)
            xp0, _ = store.get_record("g", "u")
            # 같은 윈도에 폭주 → 윈도 cap 으로 제한
            _flood(svc, "g", "u", 100, base=10.0, step=0.05, now=2.0)
            xp1, _ = store.get_record("g", "u")
            max_window = PENALTY_WINDOW_CAP * abs(scoring.PENALTY_FLOOD)
            assert (xp0 - xp1) <= max_window  # 윈도당 cap 초과 차감 불가
            assert xp1 < xp0  # 일부 차감은 발생
        finally:
            store.close()


def test_daily_cap_bounds_across_many_windows():
    # 윈도를 띄워 여러 번 시도해도 일일 cap 을 넘는 차감은 불가.
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp)
        try:
            store.add_xp("g", "u", 100_000, now=1.0)
            xp0, _ = store.get_record("g", "u")
            # 100개 윈도(각 윈도는 PENALTY_WINDOW_SECONDS=60s 간격), 윈도마다 폭주
            t = 0.0
            for _w in range(100):
                _flood(svc, "g", "u", 10, base=t, step=0.05, now=2.0)
                t += 120.0  # 다음 윈도로 점프(윈도/쿨다운 모두 경과)
            xp1, _ = store.get_record("g", "u")
            max_daily = PENALTY_DAILY_CAP * abs(scoring.PENALTY_FLOOD)
            assert (xp0 - xp1) <= max_daily  # 같은 UTC-day 일일 cap 초과 불가
        finally:
            store.close()


# ── 격리/우회 ───────────────────────────────────────────────────────


def test_flood_in_one_guild_does_not_penalize_same_user_in_other_guild():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp)
        try:
            store.add_xp("g1", "u", 300, now=1.0)
            store.add_xp("g2", "u", 300, now=1.0)
            _flood(svc, "g1", "u", 50)
            xp1, _ = store.get_record("g1", "u")
            xp2, _ = store.get_record("g2", "u")
            assert xp1 < 300  # g1 은 페널티
            assert xp2 == 300  # g2 는 무영향(길드 격리)
        finally:
            store.close()


def test_admin_immune_under_combined_flood_mention_caps():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp)
        try:
            store.add_xp("g", "a", 300, now=1.0)
            xp0, _ = store.get_record("g", "a")
            for i in range(50):
                svc.record_message(
                    "g", "a", "STOP SPAMMING EVERYONE RIGHT NOW",
                    monotonic_time=10.0 + i * 0.1, now=2.0, mention_count=20, is_admin=True,
                )
            xp1, _ = store.get_record("g", "a")
            assert xp1 >= xp0  # admin 면제 — 어떤 신호 조합에도 차감 없음
        finally:
            store.close()


def test_shadow_never_deducts_under_extreme_load():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp, leveling_decay_shadow=True)
        try:
            store.add_xp("g", "u", 300, now=1.0)
            xp0, _ = store.get_record("g", "u")
            for i in range(100):
                svc.record_message(
                    "g", "u", "STOP SPAMMING EVERYONE", monotonic_time=10.0 + i * 0.1,
                    now=2.0, mention_count=20,
                )
            xp1, _ = store.get_record("g", "u")
            assert xp1 >= xp0  # shadow: 로그만, 절대 차감 없음
        finally:
            store.close()


# ── 핫패스 read 0 (combined load) ───────────────────────────────────


def test_hot_path_zero_store_reads_under_combined_signals():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp)
        reads = {"n": 0}
        og_r, og_d = store.get_record, store.get_daily_usage
        store.get_record = lambda *a, **k: (reads.__setitem__("n", reads["n"] + 1), og_r(*a, **k))[1]
        store.get_daily_usage = lambda *a, **k: (reads.__setitem__("n", reads["n"] + 1), og_d(*a, **k))[1]
        try:
            for i in range(60):
                svc.record_message(
                    "g", "u", "STOP SPAMMING EVERYONE NOW", monotonic_time=10.0 + i * 0.1,
                    now=2.0, mention_count=8,
                )
            assert reads["n"] == 0  # 적립+페널티 경로 모두 LevelingStore read 0
        finally:
            store.get_record, store.get_daily_usage = og_r, og_d
            store.close()


def test_korean_and_short_caps_never_penalized():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp, leveling_cooldown_seconds=0.0)
        try:
            # 한글 장문(대소문자 없음) + 짧은 약어 대문자 — caps 페널티 대상 아님.
            for i, msg in enumerate(["정말 감사합니다 여러분 오늘 즐거웠어요", "OK", "LGTM", "ㄱㄱ"]):
                store.add_xp("g", f"u{i}", 200, now=1.0)
                svc.record_message("g", f"u{i}", msg, monotonic_time=1.0 + i, now=2.0)
                xp, _ = store.get_record("g", f"u{i}")
                assert xp >= 200  # 차감 없음(오히려 적립 가능)
        finally:
            store.close()
