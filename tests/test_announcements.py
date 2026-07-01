"""service/announcements.py 테스트 — cron 매칭·is_due·저장소 CRUD (discord-free)."""
from __future__ import annotations

from datetime import datetime

import pytest

from dcm.service.announcements import (
    EVENT_DEFAULT_LEADS,
    KST,
    Announcement,
    AnnouncementStore,
    CronError,
    Event,
    EventStore,
    cron_matches,
    due_event_leads,
    is_due,
    minute_key,
    parse_cron,
    render_event_message,
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


# ── 행사 카운트다운 (Event) ─────────────────────────────────────────────
DAY = 86400


def _event(**kw) -> Event:
    base = dict(
        id=1,
        guild_id="g1",
        channel_id="c",
        title="정기모임",
        event_at=1_000_000.0,
        lead_days=(30, 14, 7, 3, 1, 0),
        fired_leads=frozenset(),
        message=None,
        mention=None,
        enabled=True,
        created_by=None,
        created_at=0.0,
    )
    base.update(kw)
    return Event(**base)


def test_due_event_leads_fires_at_each_trigger():
    at = 2_000_000_000.0
    e = _event(event_at=at, lead_days=(14, 7, 3, 0), fired_leads=frozenset())
    assert due_event_leads(e, at - 15 * DAY) == []  # 아직 D-14 전
    assert due_event_leads(e, at - 14 * DAY) == [14]  # D-14 트리거 도달
    assert due_event_leads(e, at - 3 * DAY) == [14, 7, 3]  # 아직 아무것도 안 쐈다면 누적
    assert due_event_leads(e, at) == [14, 7, 3, 0]  # D-DAY


def test_due_event_leads_skips_fired():
    at = 2_000_000_000.0
    e = _event(event_at=at, lead_days=(14, 7, 3, 0), fired_leads=frozenset({14, 7}))
    assert due_event_leads(e, at - 3 * DAY) == [3]  # 이미 쏜 14/7 제외


def test_due_event_leads_disabled_and_after_event():
    at = 2_000_000_000.0
    assert due_event_leads(_event(event_at=at, enabled=False), at) == []
    e = _event(event_at=at, lead_days=(3, 0), fired_leads=frozenset())
    assert due_event_leads(e, at + 2 * 3600) == []  # 행사+1h 유예 지나면 발화 안 함


def test_event_store_late_registration_fires_current_status(tmp_path):
    # 20일 남은 시점 등록 → 지난 D-30 은 스킵, 지금 상태(D-20)를 즉시 1회 발화용으로 남김.
    st = EventStore(str(tmp_path / "e.db"))
    now = 1_000_000_000.0
    at = now + 20 * DAY
    eid = st.add(guild_id="g1", channel_id="c", title="OT", event_at=at, now=now)
    e = st.list_for_guild("g1")[0]
    assert e.id == eid
    assert 30 in e.fired_leads  # 지난 D-30 마일스톤은 스킵
    assert due_event_leads(e, now) == [20]  # 등록 직후 틱에 지금 상태(D-20) 즉시 1회
    assert 20 not in e.fired_leads and 14 not in e.fired_leads


def test_event_store_register_3days_before(tmp_path):
    # 사용자 시나리오: 행사 3일 전에 등록 → D-3 즉시, 이후 D-1/D-DAY.
    st = EventStore(str(tmp_path / "e.db"))
    now = 1_000_000_000.0
    at = now + 3 * DAY
    st.add(guild_id="g1", channel_id="c", title="OT", event_at=at, now=now)
    e = st.list_for_guild("g1")[0]
    assert due_event_leads(e, now) == [3]  # 지금 D-3 즉시 발화
    assert sorted(d for d in e.lead_days if d not in e.fired_leads) == [0, 1, 3]  # 이후 D-1/D-DAY
    assert {30, 14, 7} <= e.fired_leads  # 지난 마일스톤 스킵


def test_event_store_register_just_after_milestone_no_double(tmp_path):
    # 행사 시각이 저녁인데 D-3 트리거(저녁) 살짝 지나서 등록 → D-3 한 번만(중복 없음).
    st = EventStore(str(tmp_path / "e.db"))
    now = 1_000_000_000.0
    at = now + 3 * DAY - 2 * 3600  # D-3 트리거를 2시간 지난 시점
    st.add(guild_id="g1", channel_id="c", title="OT", event_at=at, now=now)
    e = st.list_for_guild("g1")[0]
    # 즉시 발화 1건만, 나머지 pending 은 D-1/D-DAY (D-3 중복 발화 없음)
    due_now = due_event_leads(e, now)
    assert len(due_now) == 1
    pending = sorted(d for d in e.lead_days if d not in e.fired_leads)
    assert pending == [0, 1, due_now[0]] and due_now[0] not in (0, 1)


def test_event_store_advance_registration_no_immediate(tmp_path):
    # 첫 마일스톤(D-30) 전에 등록 → 즉시 공지 없음, 마일스톤대로.
    st = EventStore(str(tmp_path / "e.db"))
    now = 1_000_000_000.0
    at = now + 45 * DAY
    st.add(guild_id="g1", channel_id="c", title="OT", event_at=at, now=now)
    e = st.list_for_guild("g1")[0]
    assert e.fired_leads == frozenset()  # 아무것도 스킵/발화 안 함
    assert due_event_leads(e, now) == []  # 즉시 발화 없음


def test_event_store_past_event_skips_all(tmp_path):
    # 행사가 이미 (유예 넘어) 지난 뒤 등록 → 전부 스킵, 발화 없음.
    st = EventStore(str(tmp_path / "e.db"))
    now = 1_000_000_000.0
    at = now - 2 * DAY
    st.add(guild_id="g1", channel_id="c", title="지난행사", event_at=at, now=now)
    e = st.list_for_guild("g1")[0]
    assert e.fired_leads == frozenset(e.lead_days)  # 전부 prefire
    assert due_event_leads(e, now) == []


def test_event_store_crud_and_mark_lead_fired(tmp_path):
    st = EventStore(str(tmp_path / "e.db"))
    now = 1_000_000_000.0
    at = now + 40 * DAY
    eid = st.add(
        guild_id="g1", channel_id="c", title="정기총회", event_at=at,
        lead_days=(30, 7, 0), message="본관 3층", mention="@everyone", created_by="u1", now=now,
    )
    e = st.list_for_guild("g1")[0]
    assert e.lead_days == (30, 7, 0) and e.message == "본관 3층" and e.mention == "@everyone"
    assert e.fired_leads == frozenset()  # 40일 후라 아무 리드도 안 지남
    st.mark_lead_fired(eid, 30)
    assert 30 in st.list_for_guild("g1")[0].fired_leads
    assert st.set_enabled(eid, "g1", False)
    assert not st.list_enabled()
    assert not st.remove(eid, "g2")  # 다른 길드는 못 지움
    assert st.remove(eid, "g1")
    assert st.list_for_guild("g1") == []
    st.close()


def test_event_store_default_leads(tmp_path):
    st = EventStore(str(tmp_path / "e.db"))
    now = 1_000_000_000.0
    st.add(guild_id="g1", channel_id="c", title="x", event_at=now + 365 * DAY, now=now)
    assert st.list_for_guild("g1")[0].lead_days == EVENT_DEFAULT_LEADS
    st.close()


def test_render_event_message_tag_and_body():
    at = datetime(2026, 7, 15, 19, 0, tzinfo=KST).timestamp()
    e = _event(event_at=at, title="여름 OT", message="음성방 집합", mention="@everyone")
    d14 = render_event_message(e, 14)
    assert "D-14" in d14 and "여름 OT" in d14 and "2026-07-15" in d14
    assert "음성방 집합" in d14 and d14.startswith("@everyone")
    dday = render_event_message(_event(event_at=at, title="여름 OT"), 0)
    assert "D-DAY" in dday and "@everyone" not in dday
