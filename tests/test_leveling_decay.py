"""신뢰-하락(Trust Decay) Phase 1 테스트.

penalty_weight 순수함수(AC1)·하한0(AC2/AC2b는 store 테스트)·강등(AC3)·오탐 가드(AC6)·
핫패스 DB read 0(AC8)·자연 회복(AC11). reconcile 는 Phase1 에서 호출하지 않는다.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dcm.leveling import scoring
from dcm.leveling.scoring import PENALTY_INJECTION, caps_ratio, cum_cost, danger_score, level, penalty_weight
from dcm.leveling.service import PENALTY_WINDOW_CAP, LevelingService
from dcm.leveling.store import LevelingStore
from dcm.memory.ingest import _parse_items
from dcm.service.guild_settings import GuildSettings, GuildSettingsStore

_LONG = "오늘 회의 자료 정리해서 공유드립니다 확인 부탁드려요"
_SPAM = "ㅋㅋㅋㅋㅋ"  # W_SPAM(0.2) → +3 적립, ASCII 알파벳 없음(caps_ratio 0)


# ───────────────────── AC1: penalty_weight 순수함수 (의존성 0) ─────────────────────


def test_penalty_weight_normal_message_is_zero():
    assert penalty_weight(_LONG, flood_count=1, mention_count=0, caps_ratio=0.0) == 0


def test_penalty_weight_flood_is_negative():
    p = penalty_weight("x", flood_count=scoring.FLOOD_THRESHOLD + 1, mention_count=0, caps_ratio=0.0)
    assert p == scoring.PENALTY_FLOOD
    assert p < 0


def test_penalty_weight_mention_burst_is_negative():
    p = penalty_weight("x", flood_count=0, mention_count=scoring.MENTION_BURST_MIN, caps_ratio=0.0)
    assert p == scoring.PENALTY_MENTION_BURST
    assert p < 0


def test_penalty_weight_caps_is_negative():
    text = "STOP SPAMMING EVERYONE RIGHT NOW"
    p = penalty_weight(text, flood_count=0, mention_count=0, caps_ratio=caps_ratio(text))
    assert p == scoring.PENALTY_CAPS
    assert p < 0


def test_penalty_weight_sums_multiple_signals():
    text = "STOP SPAMMING EVERYONE RIGHT NOW"
    p = penalty_weight(
        text,
        flood_count=scoring.FLOOD_THRESHOLD + 1,
        mention_count=scoring.MENTION_BURST_MIN,
        caps_ratio=caps_ratio(text),
    )
    assert p == scoring.PENALTY_FLOOD + scoring.PENALTY_MENTION_BURST + scoring.PENALTY_CAPS


def test_caps_ratio_korean_is_zero():
    # 한글은 대소문자가 없으므로 CAPS 페널티 대상에서 자연 제외(오탐 방지).
    assert caps_ratio("안녕하세요 반갑습니다 여러분") == 0.0


def test_caps_ratio_all_upper_is_one():
    assert caps_ratio("HELLO") == 1.0


def test_short_uppercase_not_penalized():
    # 짧은 약어/명령("OK","LGTM")은 CAPS_MIN_LEN 미만이라 페널티 없음.
    assert penalty_weight("OK", flood_count=0, mention_count=0, caps_ratio=1.0) == 0


# ───────────────────── 헬퍼 ─────────────────────


def _service(tmp, settings=None, cooldown=60.0):
    store = LevelingStore(os.path.join(tmp, "leveling.db"))
    return LevelingService(store, settings, default_cooldown=cooldown), store


class _Settings:
    """record_message 가 읽는 get(gid) 만 제공하는 settings 스텁."""

    def __init__(self, **kw):
        self._gs = GuildSettings(guild_id="g", **kw)

    def get(self, gid):
        return self._gs


# ───────────────────── AC3: penalty → 레벨 강등 ─────────────────────


def test_penalty_demotes_level():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(leveling_decay_enabled=True))
        try:
            store.add_xp("g", "u", 300, now=1.0)
            xp0, _ = store.get_record("g", "u")
            lvl0 = level(xp0)
            assert lvl0 >= 1
            for i in range(scoring.FLOOD_THRESHOLD + 2):
                svc.record_message("g", "u", _SPAM, monotonic_time=100.0 + i * 0.1, now=2.0)
            xp1, _ = store.get_record("g", "u")
            assert xp1 < xp0  # 신뢰점수 하락
            assert level(xp1) < lvl0  # 레벨 강등
        finally:
            store.close()


# ───────────────────── AC6: 오탐 가드 (admin/shadow/cap) ─────────────────────


def test_admin_is_exempt_from_penalty():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(leveling_decay_enabled=True))
        try:
            store.add_xp("g", "a", 200, now=1.0)
            xp0, _ = store.get_record("g", "a")
            for i in range(scoring.FLOOD_THRESHOLD + 5):
                svc.record_message("g", "a", _SPAM, monotonic_time=10.0 + i * 0.1, now=2.0, is_admin=True)
            xp1, _ = store.get_record("g", "a")
            assert xp1 >= xp0  # admin 은 차감 면제
        finally:
            store.close()


def test_shadow_mode_does_not_deduct():
    with tempfile.TemporaryDirectory() as tmp:
        settings = _Settings(leveling_decay_enabled=True, leveling_decay_shadow=True)
        svc, store = _service(tmp, settings)
        try:
            store.add_xp("g", "u", 200, now=1.0)
            xp0, _ = store.get_record("g", "u")
            for i in range(scoring.FLOOD_THRESHOLD + 5):
                svc.record_message("g", "u", _SPAM, monotonic_time=10.0 + i * 0.1, now=2.0)
            xp1, _ = store.get_record("g", "u")
            assert xp1 >= xp0  # shadow: 로그만, 차감 없음
        finally:
            store.close()


def test_window_cap_limits_deduction():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(leveling_decay_enabled=True))
        try:
            store.add_xp("g", "u", 1000, now=1.0)
            xp0, _ = store.get_record("g", "u")
            for i in range(30):  # 윈도 내 대량 플러딩
                svc.record_message("g", "u", _SPAM, monotonic_time=10.0 + i * 0.1, now=2.0)
            xp1, _ = store.get_record("g", "u")
            max_deduct = PENALTY_WINDOW_CAP * abs(scoring.PENALTY_FLOOD)
            assert xp1 < xp0  # 차감은 발생
            assert xp1 >= xp0 - max_deduct  # 그러나 cap 으로 제한(드레인 방지)
        finally:
            store.close()


def test_decay_disabled_by_default_no_penalty():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, None)  # settings None → decay OFF(기본값)
        try:
            store.add_xp("g", "u", 200, now=1.0)
            xp0, _ = store.get_record("g", "u")
            for i in range(scoring.FLOOD_THRESHOLD + 5):
                svc.record_message("g", "u", _SPAM, monotonic_time=10.0 + i * 0.1, now=2.0)
            xp1, _ = store.get_record("g", "u")
            assert xp1 >= xp0  # 기존 동작 유지(차감 없음)
        finally:
            store.close()


# ───────────────────── AC8: 핫패스 DB read 0 (penalty 경로 포함) ─────────────────────


def test_hot_path_no_db_reads_even_with_penalty():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(leveling_decay_enabled=True))
        reads = {"n": 0}
        orig_get_record = store.get_record
        orig_get_daily = store.get_daily_usage

        def counting_get_record(*a, **k):
            reads["n"] += 1
            return orig_get_record(*a, **k)

        def counting_get_daily(*a, **k):
            reads["n"] += 1
            return orig_get_daily(*a, **k)

        store.get_record = counting_get_record
        store.get_daily_usage = counting_get_daily
        try:
            for i in range(scoring.FLOOD_THRESHOLD + 8):  # 정상 + 플러딩 페널티
                svc.record_message("g", "u", _SPAM, monotonic_time=10.0 + i * 0.1, now=2.0)
            assert reads["n"] == 0  # 적립/페널티 모두 인메모리+write, read 0
        finally:
            store.get_record = orig_get_record
            store.get_daily_usage = orig_get_daily
            store.close()


# ───────────────────── AC11: 바닥(0)에서 자연 회복 ─────────────────────


def test_natural_recovery_from_floor():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(leveling_decay_enabled=True), cooldown=0.0)
        try:
            assert store.get_record("g", "u") == (0, 0.0)  # 바닥
            assert svc.record_message("g", "u", _LONG, monotonic_time=1.0, now=1.0) is True
            xp, _ = store.get_record("g", "u")
            assert xp == 15  # 정상 메시지 1건으로 즉시 회복(메시지 기반)
        finally:
            store.close()


# ───────────────── guild_settings: decay 토글 round-trip + 마이그레이션 ─────────────────


def test_guild_settings_decay_roundtrip_and_migration():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "memory.db")
        store = GuildSettingsStore(path)
        try:
            s0 = store.get("g1")
            assert s0.leveling_decay_enabled is None  # 기본 미설정(=서비스 기본 OFF)
            assert s0.leveling_decay_shadow is None
            store.set_leveling_decay_enabled("g1", True)
            store.set_leveling_decay_shadow("g1", True)
            s1 = store.get("g1")
            assert s1.leveling_decay_enabled is True
            assert s1.leveling_decay_shadow is True
        finally:
            store.close()
        # 재오픈 — _migrate 는 idempotent(이미 존재하는 컬럼에 ALTER 하지 않음).
        store2 = GuildSettingsStore(path)
        try:
            assert store2.get("g1").leveling_decay_enabled is True
        finally:
            store2.close()


# ───────────────── AC7: penalty/shadow 전량 audit (caplog) ─────────────────


def test_penalty_emits_audit_log(caplog):
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(leveling_decay_enabled=True))
        try:
            store.add_xp("g", "u", 300, now=1.0)
            with caplog.at_level(logging.WARNING):
                for i in range(scoring.FLOOD_THRESHOLD + 2):
                    svc.record_message("g", "u", _SPAM, monotonic_time=100.0 + i * 0.1, now=2.0)
            recs = [r.getMessage() for r in caplog.records if "decay penalty" in r.getMessage()]
            assert recs  # 차감은 audit 로그로 남는다
            assert "guild=g" in recs[0] and "user=u" in recs[0] and "delta=" in recs[0]
        finally:
            store.close()


def test_shadow_emits_would_penalize_log(caplog):
    with tempfile.TemporaryDirectory() as tmp:
        settings = _Settings(leveling_decay_enabled=True, leveling_decay_shadow=True)
        svc, store = _service(tmp, settings)
        try:
            store.add_xp("g", "u", 300, now=1.0)
            with caplog.at_level(logging.INFO):
                for i in range(scoring.FLOOD_THRESHOLD + 2):
                    svc.record_message("g", "u", _SPAM, monotonic_time=100.0 + i * 0.1, now=2.0)
            recs = [r.getMessage() for r in caplog.records if "SHADOW" in r.getMessage()]
            assert recs  # shadow 모드도 would-penalize audit 로그를 남긴다
        finally:
            store.close()


# ───────────────── AC5: 티어 경계 강등만 게이팅에 영향 ─────────────────


def test_penalty_crossing_tier_boundary_reduces_quota():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(leveling_decay_enabled=True))
        try:
            store.add_xp("g", "u", cum_cost(5) + 5, now=1.0)  # 레벨 5 (web 한도 50)
            _a0, rem0 = svc.quota_check("g", "u", "web", day="2099-01-01")
            assert rem0 == 50
            for i in range(scoring.FLOOD_THRESHOLD + 2):  # 경계 아래로 강등
                svc.record_message("g", "u", _SPAM, monotonic_time=100.0 + i * 0.1, now=2.0)
            _a1, rem1 = svc.quota_check("g", "u", "web", day="2099-01-01")
            assert rem1 == 20  # 티어 경계 강등 → web 한도 50→20
            assert rem1 < rem0
        finally:
            store.close()


def test_penalty_within_tier_does_not_change_quota():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(leveling_decay_enabled=True))
        try:
            store.add_xp("g", "u", cum_cost(7) + 10, now=1.0)  # 레벨 7 (web 한도 50)
            _a0, rem0 = svc.quota_check("g", "u", "web", day="2099-01-01")
            assert rem0 == 50
            for i in range(scoring.FLOOD_THRESHOLD + 2):
                svc.record_message("g", "u", _SPAM, monotonic_time=100.0 + i * 0.1, now=2.0)
            _a1, rem1 = svc.quota_check("g", "u", "web", day="2099-01-01")
            assert rem1 == rem0  # 같은 quota 티어(레벨 5~9) 내 변동 → 게이팅 무변
        finally:
            store.close()


# ───────────────── Phase 2: 인젝션 신호 / 위험 워드리스트 / on_penalty ─────────────────


def test_parse_items_envelope_with_injection_flag():
    items, injection = _parse_items('{"memories": [{"content": "x", "importance": 3}], "injection": true}')
    assert injection is True
    assert len(items) == 1
    items2, inj2 = _parse_items('[{"content": "y", "importance": 2}]')  # 레거시 배열
    assert inj2 is False
    assert len(items2) == 1


def test_danger_score_pure():
    assert danger_score("free nitro here") == scoring.PENALTY_DANGER
    assert danger_score("정상적인 대화입니다 여러분") == 0


def test_apply_signal_penalty_injection_when_enabled():
    with tempfile.TemporaryDirectory() as tmp:
        settings = _Settings(leveling_decay_enabled=True, leveling_injection_enabled=True)
        svc, store = _service(tmp, settings)
        try:
            store.add_xp("g", "u", 300, now=1.0)
            assert svc.apply_signal_penalty("g", "u", PENALTY_INJECTION, signal="injection", now=2.0) is True
            xp, _ = store.get_record("g", "u")
            assert xp == 300 + PENALTY_INJECTION  # 300 - 60
        finally:
            store.close()


def test_apply_signal_penalty_gated_off_when_injection_disabled():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(leveling_decay_enabled=True))  # injection 토글 off
        try:
            store.add_xp("g", "u", 300, now=1.0)
            assert svc.apply_signal_penalty("g", "u", PENALTY_INJECTION, signal="injection", now=2.0) is False
            xp, _ = store.get_record("g", "u")
            assert xp == 300  # 차감 없음
        finally:
            store.close()


def test_apply_signal_penalty_shadow_does_not_deduct():
    with tempfile.TemporaryDirectory() as tmp:
        settings = _Settings(
            leveling_decay_enabled=True, leveling_injection_enabled=True, leveling_decay_shadow=True
        )
        svc, store = _service(tmp, settings)
        try:
            store.add_xp("g", "u", 300, now=1.0)
            assert svc.apply_signal_penalty("g", "u", PENALTY_INJECTION, signal="injection", now=2.0) is False
            xp, _ = store.get_record("g", "u")
            assert xp == 300  # shadow: 로그만, 차감 없음
        finally:
            store.close()


def test_danger_wordlist_penalizes_when_enabled():
    with tempfile.TemporaryDirectory() as tmp:
        settings = _Settings(leveling_decay_enabled=True, leveling_danger_enabled=True)
        svc, store = _service(tmp, settings)
        try:
            store.add_xp("g", "u", 300, now=1.0)
            svc.record_message(
                "g", "u", "FREE NITRO claim steamcommunity.com/gift/xyz", monotonic_time=10.0, now=2.0
            )
            xp, _ = store.get_record("g", "u")
            assert xp < 300  # 위험(스캠) 콘텐츠 차감
        finally:
            store.close()


def test_danger_disabled_by_default_no_penalty():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(leveling_decay_enabled=True))  # danger 토글 off
        try:
            store.add_xp("g", "u", 300, now=1.0)
            svc.record_message(
                "g", "u", "free nitro steamcommunity.com/gift/x", monotonic_time=10.0, now=2.0
            )
            xp, _ = store.get_record("g", "u")
            assert xp >= 300  # danger off → 차감 없음
        finally:
            store.close()


def test_on_penalty_callback_fires_on_enforced_penalty():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _service(tmp, _Settings(leveling_decay_enabled=True))
        try:
            fired = []
            for i in range(scoring.FLOOD_THRESHOLD + 2):
                svc.record_message(
                    "g", "u", _SPAM, monotonic_time=100.0 + i * 0.1, now=2.0,
                    on_penalty=lambda: fired.append(1),
                )
            assert fired  # 실제 차감 시 on_penalty 트리거(→ adapter reconcile/revoke)
        finally:
            store.close()
