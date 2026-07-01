"""예약 공지 저장소 + cron 스케줄 로직 (discord-free, 순수 모듈).

관리봇 용도: 특정 일정(주간 회의·이벤트 리마인더 등)을 지정 채널에 주기적으로/1회성으로 공지.
- 반복: 5필드 cron 표현식(분 시 일 월 요일), **KST(Asia/Seoul)** 기준. 새 의존성 없이 자체 매처.
- 1회성: run_at(epoch, UTC)에 한 번.
어댑터(platform)가 매분 틱마다 due_now() 로 발화 대상을 골라 채널에 게시하고 mark_fired 한다.
cron 문법: `*`, `N`, `*/N`, `A-B`, 콤마 목록. 요일 0/7=일요일. 표준 vixie-cron 축약만(L/W/# 미지원).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_announcements (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id          TEXT NOT NULL,
  channel_id        TEXT NOT NULL,
  message           TEXT NOT NULL,
  cron              TEXT,            -- 반복: 5필드 cron (KST). NULL 이면 1회성.
  run_at            REAL,            -- 1회성: epoch(UTC). NULL 이면 반복.
  enabled           INTEGER NOT NULL DEFAULT 1,
  last_fired_minute TEXT,            -- 중복 발화 방지용 KST 'YYYY-MM-DD HH:MM'
  created_by        TEXT,
  created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ann_guild ON scheduled_announcements(guild_id);
"""

_EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id    TEXT NOT NULL,
  channel_id  TEXT NOT NULL,
  title       TEXT NOT NULL,
  event_at    REAL NOT NULL,             -- 행사 시각 epoch(UTC, KST 로 입력)
  lead_days   TEXT NOT NULL,             -- 카운트다운 오프셋 CSV, 예 '30,14,7,3,1,0'
  fired_leads TEXT NOT NULL DEFAULT '',  -- 이미 발화한 오프셋 CSV
  message     TEXT,                      -- 선택 추가 문구
  mention     TEXT,                      -- 선택 멘션(@everyone / <@&role>)
  enabled     INTEGER NOT NULL DEFAULT 1,
  created_by  TEXT,
  created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evt_guild ON scheduled_events(guild_id);
"""

# 행사 카운트다운 기본 리드데이(일): 한 달·2주·1주·3일·1일 전 + 당일.
EVENT_DEFAULT_LEADS: tuple[int, ...] = (30, 14, 7, 3, 1, 0)

_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))  # 분 시 일 월 요일


@dataclass(frozen=True)
class Announcement:
    id: int
    guild_id: str
    channel_id: str
    message: str
    cron: str | None
    run_at: float | None
    enabled: bool
    last_fired_minute: str | None
    created_by: str | None
    created_at: float


class CronError(ValueError):
    """잘못된 cron 표현식."""


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    """cron 한 필드를 허용값 집합으로. `*`, `N`, `*/N`, `A-B`, `A-B/N`, 콤마 목록 지원."""
    out: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"빈 필드 항목: {field!r}")
        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError(f"잘못된 step: {part!r}")
            step = int(step_s)
        else:
            base = part
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, _, b = base.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"잘못된 범위: {part!r}")
            start, end = int(a), int(b)
        elif base.isdigit():
            start = end = int(base)
        else:
            raise CronError(f"잘못된 값: {part!r}")
        if start < lo or end > hi or start > end:
            raise CronError(f"범위 벗어남: {part!r} (허용 {lo}-{hi})")
        out.update(range(start, end + 1, step))
    return out


def parse_cron(expr: str) -> list[set[int]]:
    """5필드 cron 을 필드별 허용값 집합 리스트로 파싱(검증 겸용). 실패 시 CronError."""
    fields = (expr or "").split()
    if len(fields) != 5:
        raise CronError("cron 은 5필드여야 함: '분 시 일 월 요일' (예: '0 9 * * 1')")
    return [_parse_field(f, lo, hi) for f, (lo, hi) in zip(fields, _FIELD_BOUNDS)]


def cron_matches(expr: str, dt: datetime) -> bool:
    """dt(해당 타임존의 시각)가 cron 에 매칭되는지. 일/요일은 vixie-cron 세만틱스(둘 중 하나라도
    제약이면 OR; 둘 다 * 면 항상 매칭)."""
    minute, hour, dom, month, dow = parse_cron(expr)
    py_dow = (dt.weekday() + 1) % 7  # Mon=0(py) → cron Sun=0
    if dt.minute not in minute or dt.hour not in hour or dt.month not in month:
        return False
    dom_restricted = dom != set(range(1, 32))
    dow_restricted = dow != set(range(0, 7))
    dom_ok = dt.day in dom
    dow_ok = py_dow in dow
    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def minute_key(now_utc: float) -> str:
    """발화 중복 방지용 KST 분 단위 키."""
    return datetime.fromtimestamp(now_utc, KST).strftime("%Y-%m-%d %H:%M")


def is_due(ann: Announcement, now_utc: float) -> bool:
    """이 공지가 지금 발화해야 하는지(순수 판정)."""
    if not ann.enabled:
        return False
    key = minute_key(now_utc)
    if ann.cron:
        if ann.last_fired_minute == key:  # 이번 분에 이미 발화
            return False
        return cron_matches(ann.cron, datetime.fromtimestamp(now_utc, KST))
    if ann.run_at is not None:  # 1회성
        return ann.last_fired_minute is None and now_utc >= ann.run_at
    return False


@dataclass(frozen=True)
class Event:
    """행사 일정 1건. event_at 시점 기준으로 lead_days(일)만큼 앞서 카운트다운 공지."""

    id: int
    guild_id: str
    channel_id: str
    title: str
    event_at: float
    lead_days: tuple[int, ...]
    fired_leads: frozenset[int]
    message: str | None
    mention: str | None
    enabled: bool
    created_by: str | None
    created_at: float


def due_event_leads(evt: Event, now_utc: float) -> list[int]:
    """지금 발화해야 할 리드 오프셋 목록(아직 안 쏜 것 중 트리거 도달, 행사 직후 1시간까지)."""
    if not evt.enabled:
        return []
    out: list[int] = []
    for lead in evt.lead_days:
        if lead in evt.fired_leads:
            continue
        trigger = evt.event_at - lead * 86400
        if trigger <= now_utc <= evt.event_at + 3600:
            out.append(lead)
    return out


def render_event_message(evt: Event, lead: int) -> str:
    """행사 카운트다운 공지 문구(discord-free). D-N / D-DAY 태그 + 일시 + 선택 문구/멘션."""
    when = datetime.fromtimestamp(evt.event_at, KST)
    dow = "월화수목금토일"[when.weekday()]
    tag = "🔔 **D-DAY**" if lead == 0 else f"🔔 **D-{lead}**"
    lines = [
        f"{tag} · 📅 **{evt.title}**",
        f"🗓️ {when.strftime('%Y-%m-%d')} ({dow}) {when.strftime('%H:%M')} (KST)",
    ]
    if evt.message:
        lines.append(evt.message)
    body = "\n".join(lines)
    if evt.mention:
        body = f"{evt.mention}\n{body}"
    return body


class AnnouncementStore:
    """예약 공지 SQLite 저장소 (memory.db 동일 파일 재사용 가능). discord-free."""

    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def _row(self, r) -> Announcement:
        return Announcement(
            id=int(r["id"]),
            guild_id=r["guild_id"],
            channel_id=r["channel_id"],
            message=r["message"],
            cron=r["cron"],
            run_at=r["run_at"],
            enabled=bool(r["enabled"]),
            last_fired_minute=r["last_fired_minute"],
            created_by=r["created_by"],
            created_at=r["created_at"],
        )

    def add(self, *, guild_id, channel_id, message, cron=None, run_at=None, created_by=None, now=None) -> int:
        import time as _t

        if not cron and run_at is None:
            raise ValueError("cron 또는 run_at 중 하나는 필요")
        if cron:
            parse_cron(cron)  # 검증
        cur = self._db.execute(
            "INSERT INTO scheduled_announcements "
            "(guild_id, channel_id, message, cron, run_at, enabled, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (str(guild_id), str(channel_id), message, cron, run_at, str(created_by) if created_by else None,
             now if now is not None else _t.time()),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def list_for_guild(self, guild_id) -> list[Announcement]:
        rows = self._db.execute(
            "SELECT * FROM scheduled_announcements WHERE guild_id = ? ORDER BY id", (str(guild_id),)
        ).fetchall()
        return [self._row(r) for r in rows]

    def list_enabled(self) -> list[Announcement]:
        rows = self._db.execute(
            "SELECT * FROM scheduled_announcements WHERE enabled = 1"
        ).fetchall()
        return [self._row(r) for r in rows]

    def remove(self, ann_id: int, guild_id) -> bool:
        cur = self._db.execute(
            "DELETE FROM scheduled_announcements WHERE id = ? AND guild_id = ?", (int(ann_id), str(guild_id))
        )
        self._db.commit()
        return cur.rowcount > 0

    def set_enabled(self, ann_id: int, guild_id, enabled: bool) -> bool:
        cur = self._db.execute(
            "UPDATE scheduled_announcements SET enabled = ? WHERE id = ? AND guild_id = ?",
            (1 if enabled else 0, int(ann_id), str(guild_id)),
        )
        self._db.commit()
        return cur.rowcount > 0

    def mark_fired(self, ann_id: int, key: str) -> None:
        self._db.execute(
            "UPDATE scheduled_announcements SET last_fired_minute = ? WHERE id = ?", (key, int(ann_id))
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()


class EventStore:
    """행사 일정(카운트다운 공지) SQLite 저장소. memory.db 동일 파일 재사용. discord-free."""

    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_EVENT_SCHEMA)
        self._db.commit()

    @staticmethod
    def _ints(csv: str) -> list[int]:
        return [int(x) for x in (csv or "").split(",") if x.strip() != ""]

    def _row(self, r) -> Event:
        return Event(
            id=int(r["id"]),
            guild_id=r["guild_id"],
            channel_id=r["channel_id"],
            title=r["title"],
            event_at=float(r["event_at"]),
            lead_days=tuple(self._ints(r["lead_days"])),
            fired_leads=frozenset(self._ints(r["fired_leads"])),
            message=r["message"],
            mention=r["mention"],
            enabled=bool(r["enabled"]),
            created_by=r["created_by"],
            created_at=r["created_at"],
        )

    def add(
        self,
        *,
        guild_id,
        channel_id,
        title,
        event_at: float,
        lead_days=EVENT_DEFAULT_LEADS,
        message=None,
        mention=None,
        created_by=None,
        now=None,
    ) -> int:
        import time as _t

        now_ts = now if now is not None else _t.time()
        leads = tuple(sorted({int(x) for x in lead_days}, reverse=True))
        if any(d < 0 for d in leads):
            raise ValueError("lead_days 는 음수 불가")
        # 이미 트리거가 지난(놓친) 오프셋은 미리 발화 처리 → 등록 즉시 과거 알림 폭주 방지.
        prefired = sorted((d for d in leads if event_at - d * 86400 <= now_ts), reverse=True)
        cur = self._db.execute(
            "INSERT INTO scheduled_events "
            "(guild_id, channel_id, title, event_at, lead_days, fired_leads, message, mention, "
            " enabled, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                str(guild_id),
                str(channel_id),
                title,
                float(event_at),
                ",".join(str(d) for d in leads),
                ",".join(str(d) for d in prefired),
                message,
                mention,
                str(created_by) if created_by else None,
                now_ts,
            ),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def list_for_guild(self, guild_id) -> list[Event]:
        rows = self._db.execute(
            "SELECT * FROM scheduled_events WHERE guild_id = ? ORDER BY event_at", (str(guild_id),)
        ).fetchall()
        return [self._row(r) for r in rows]

    def list_enabled(self) -> list[Event]:
        rows = self._db.execute("SELECT * FROM scheduled_events WHERE enabled = 1").fetchall()
        return [self._row(r) for r in rows]

    def remove(self, event_id: int, guild_id) -> bool:
        cur = self._db.execute(
            "DELETE FROM scheduled_events WHERE id = ? AND guild_id = ?", (int(event_id), str(guild_id))
        )
        self._db.commit()
        return cur.rowcount > 0

    def set_enabled(self, event_id: int, guild_id, enabled: bool) -> bool:
        cur = self._db.execute(
            "UPDATE scheduled_events SET enabled = ? WHERE id = ? AND guild_id = ?",
            (1 if enabled else 0, int(event_id), str(guild_id)),
        )
        self._db.commit()
        return cur.rowcount > 0

    def mark_lead_fired(self, event_id: int, lead: int) -> None:
        row = self._db.execute(
            "SELECT fired_leads FROM scheduled_events WHERE id = ?", (int(event_id),)
        ).fetchone()
        if row is None:
            return
        fired = self._ints(row["fired_leads"])
        if int(lead) not in fired:
            fired.append(int(lead))
        self._db.execute(
            "UPDATE scheduled_events SET fired_leads = ? WHERE id = ?",
            (",".join(str(d) for d in sorted(set(fired), reverse=True)), int(event_id)),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()
