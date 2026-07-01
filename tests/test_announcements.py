"""service/announcements.py 테스트 — cron 매칭·is_due·저장소 CRUD (discord-free)."""
from __future__ import annotations

from datetime import datetime

import pytest

from dcm.service.announcements import (
    KST,
    Announcement,
    AnnouncementStore,
    CronError,
    cron_matches,
    is_due,
    minute_key,
    parse_cron,
)


# ── cron 파싱/검증 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["", "0 9 * *", "0 9 * * * *", "60 9 * * *", "0 24 * * *", "a 9 * * *", "0 9 0 * *", "0 9 * 13 *", "0 9 * * 8"])
def test_parse_cron_invalid(bad):
    with pytest.raises(CronError):
        parse_cron(bad)


def test_parse_cron_valid():
    assert parse_cron("0 9 * * 1")  # 매주 월 09:00
    assert parse_cron("*/15 * * * *")
    assert parse_cron("0,30 8-18 * * 1-5")


# ── cron 매칭 ───────────────────────────────────────────────────────────
def test_cron_matches_minute_hour():
    dt = datetime(2026, 7, 6, 9, 30)
    assert cron_matches("* * * * *", dt)
    assert cron_matches("30 9 * * *", dt)
    assert not cron_matches("0 9 * * *", dt)
    assert not cron_matches("30 10 * * *", dt)


def test_cron_step_range_list():
    dt = datetime(2026, 7, 6, 9, 15)
    assert cron_matches("*/15 * * * *", dt)
    assert cron_matches("0-30 9 * * *", dt)
    assert cron_matches("0,15,30 9 * * *", dt)
    assert not cron_matches("*/20 * * * *", dt)


def test_cron_dow():
    dt = datetime(2026, 7, 6, 9, 0)
    cron_dow = (dt.weekday() + 1) % 7  # cron 요일(일=0)
    assert cron_matches(f"0 9 * * {cron_dow}", dt)
    assert not cron_matches(f"0 9 * * {(cron_dow + 1) % 7}", dt)
    assert cron_matches("0 9 * * 0-6", dt)  # 모든 요일


def test_cron_dom_or_dow_semantics():
    dt = datetime(2026, 7, 6, 9, 0)  # day=6
    cron_dow = (dt.weekday() + 1) % 7
    other_day = 7 if dt.day != 7 else 8
    # dom·dow 둘 다 제약이면 OR: 요일만 맞아도 매칭
    assert cron_matches(f"0 9 {other_day} * {cron_dow}", dt)
    # 둘 다 안 맞으면 미매칭
    assert not cron_matches(f"0 9 {other_day} * {(cron_dow + 1) % 7}", dt)
    # 날짜만 맞아도 매칭(OR)
    assert cron_matches(f"0 9 {dt.day} * {(cron_dow + 1) % 7}", dt)


# ── is_due ──────────────────────────────────────────────────────────────
def _ann(**kw) -> Announcement:
    base = dict(
        id=1, guild_id="g", channel_id="c", message="m", cron=None, run_at=None,
        enabled=True, last_fired_minute=None, created_by=None, created_at=0.0,
    )
    base.update(kw)
    return Announcement(**base)


def test_is_due_recurring():
    kst_dt = datetime(2026, 7, 6, 9, 0, tzinfo=KST)
    now = kst_dt.timestamp()
    cron_dow = (kst_dt.weekday() + 1) % 7
    ann = _ann(cron=f"0 9 * * {cron_dow}")
    assert is_due(ann, now)
    assert not is_due(_ann(cron=f"0 9 * * {cron_dow}", last_fired_minute=minute_key(now)), now)
    assert not is_due(_ann(cron="* * * * *", enabled=False), now)
    # 다른 분이면 미발화
    assert not is_due(_ann(cron="1 9 * * *"), now)


def test_is_due_oneshot():
    now = 1_000_000.0
    assert is_due(_ann(run_at=now - 1), now)
    assert not is_due(_ann(run_at=now + 100), now)
    assert not is_due(_ann(run_at=now - 1, last_fired_minute="x"), now)  # 이미 발화


def test_minute_key_is_kst():
    now = datetime(2026, 7, 6, 0, 0, tzinfo=KST).timestamp()
    assert minute_key(now) == "2026-07-06 00:00"


# ── 저장소 CRUD ─────────────────────────────────────────────────────────
def test_store_add_list_toggle_remove(tmp_path):
    st = AnnouncementStore(str(tmp_path / "a.db"))
    aid = st.add(guild_id="g1", channel_id="c1", message="주간 회의", cron="0 9 * * 1", created_by="u1")
    assert aid > 0
    lst = st.list_for_guild("g1")
    assert len(lst) == 1 and lst[0].cron == "0 9 * * 1" and lst[0].enabled and lst[0].message == "주간 회의"
    assert st.list_for_guild("other") == []
    assert len(st.list_enabled()) == 1
    assert st.set_enabled(aid, "g1", False)
    assert st.list_enabled() == []
    assert st.remove(aid, "g1")
    assert st.list_for_guild("g1") == []
    st.close()


def test_store_add_oneshot(tmp_path):
    st = AnnouncementStore(str(tmp_path / "a.db"))
    aid = st.add(guild_id="g", channel_id="c", message="이벤트", run_at=123456.0)
    row = st.list_for_guild("g")[0]
    assert row.run_at == 123456.0 and row.cron is None and row.id == aid
    st.close()


def test_store_add_validates(tmp_path):
    st = AnnouncementStore(str(tmp_path / "a.db"))
    with pytest.raises(CronError):
        st.add(guild_id="g", channel_id="c", message="m", cron="nope")
    with pytest.raises(ValueError):
        st.add(guild_id="g", channel_id="c", message="m")  # cron/run_at 둘 다 없음
    st.close()


def test_store_mark_fired_and_guild_scoped_remove(tmp_path):
    st = AnnouncementStore(str(tmp_path / "a.db"))
    aid = st.add(guild_id="g1", channel_id="c", message="m", cron="* * * * *")
    st.mark_fired(aid, "2026-07-06 09:00")
    assert st.list_enabled()[0].last_fired_minute == "2026-07-06 09:00"
    assert not st.remove(aid, "g2")  # 다른 길드는 못 지움
    assert len(st.list_for_guild("g1")) == 1
    st.close()
