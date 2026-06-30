"""레벨링 순수함수 테스트 (곡선·휴리스틱·경계·UTC-day)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dcm.leveling import scoring


def test_level_step_and_cum_cost_monotonic():
    assert scoring.cum_cost(0) == 0
    # level_step 은 비감소(엄밀 증가)
    prev = -1
    for n in range(0, 30):
        step = scoring.level_step(n)
        assert step > prev
        prev = step
    # cum_cost 는 엄밀 증가
    prev_c = -1
    for n in range(0, 30):
        c = scoring.cum_cost(n)
        assert c > prev_c
        prev_c = c
    # cum_cost(n+1) - cum_cost(n) == level_step(n)
    for n in range(0, 20):
        assert scoring.cum_cost(n + 1) - scoring.cum_cost(n) == scoring.level_step(n)


def test_level_boundaries():
    assert scoring.level(0) == 0
    assert scoring.level(-100) == 0  # 음수 방어
    c1 = scoring.cum_cost(1)  # 100
    assert scoring.level(c1 - 1) == 0
    assert scoring.level(c1) == 1
    assert scoring.level(c1 + 1) == 1
    c2 = scoring.cum_cost(2)
    assert scoring.level(c2 - 1) == 1
    assert scoring.level(c2) == 2


def test_progress_within_unit_interval():
    for xp in (0, 1, 50, 99, 100, 101, 254, 255, 999, 5000):
        p = scoring.progress(xp)
        assert 0.0 <= p < 1.0
    # 레벨 경계에서 progress == 0
    assert scoring.progress(scoring.cum_cost(3)) == 0.0


def test_xp_to_next_positive_and_consistent():
    for xp in (0, 50, 100, 300):
        lvl = scoring.level(xp)
        nxt = scoring.xp_to_next(xp)
        assert nxt > 0
        assert xp + nxt == scoring.cum_cost(lvl + 1)


def test_quality_weight_branches():
    assert scoring.quality_weight("") == scoring.W_EMPTY
    assert scoring.quality_weight("    ") == scoring.W_EMPTY
    assert scoring.quality_weight("ㅋㅋㅋㅋㅋㅋ") == scoring.W_SPAM
    assert scoring.quality_weight("aaaaaaaa") == scoring.W_SPAM
    assert scoring.quality_weight("🙂") == scoring.W_EMOJI_ONLY
    assert scoring.quality_weight("!?!?") == scoring.W_EMOJI_ONLY
    assert scoring.quality_weight("안녕") == scoring.W_SHORT
    assert scoring.quality_weight("hi") == scoring.W_SHORT
    assert scoring.quality_weight("오늘 회의 자료 정리해서 공유드립니다") == scoring.W_NORMAL


def test_xp_award_is_integer_and_quality_scaled():
    normal = scoring.xp_award("오늘 회의 자료 정리해서 공유드립니다")
    short = scoring.xp_award("hi")
    spam = scoring.xp_award("ㅋㅋㅋㅋㅋㅋ")
    empty = scoring.xp_award("")
    for v in (normal, short, spam, empty):
        assert isinstance(v, int)
    assert normal > short >= spam > empty
    assert empty == 0
    assert normal == 15  # base_xp * 1.0


def test_utc_day_pure_and_boundary():
    assert scoring.utc_day(0) == "1970-01-01"
    # 같은 epoch → 같은 키
    assert scoring.utc_day(1_700_000_000) == scoring.utc_day(1_700_000_000)
    # 하루(86400s) 차이 → 다른 키
    assert scoring.utc_day(0) != scoring.utc_day(86_400)
    assert scoring.utc_day(86_400) == "1970-01-02"
