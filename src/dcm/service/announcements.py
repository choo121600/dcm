"""Scheduled-announcement store + cron scheduling logic (discord-free, pure module).

For the admin bot: post a given schedule (weekly meetings, event reminders, etc.) to a designated
channel, either recurring or one-shot.
- Recurring: 5-field cron expression (minute hour day month weekday), based on **KST (Asia/Seoul)**.
  A self-contained matcher with no new dependencies.
- One-shot: once at run_at (epoch, UTC).
The adapter (platform) picks the due targets via due_now() on each minute tick, posts to the channel,
and calls mark_fired.
cron syntax: `*`, `N`, `*/N`, `A-B`, comma lists. Weekday 0/7=Sunday. Standard vixie-cron
abbreviations only (L/W/# unsupported).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..i18n import FALLBACK_LOCALE, t

KST = ZoneInfo("Asia/Seoul")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_announcements (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id          TEXT NOT NULL,
  channel_id        TEXT NOT NULL,
  message           TEXT NOT NULL,
  cron              TEXT,            -- recurring: 5-field cron (KST). NULL means one-shot.
  run_at            REAL,            -- one-shot: epoch (UTC). NULL means recurring.
  enabled           INTEGER NOT NULL DEFAULT 1,
  last_fired_minute TEXT,            -- KST 'YYYY-MM-DD HH:MM' to prevent duplicate firing
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
  event_at    REAL NOT NULL,             -- event time epoch (UTC; entered as KST)
  lead_days   TEXT NOT NULL,             -- countdown offsets CSV, e.g. '30,14,7,3,1,0'
  fired_leads TEXT NOT NULL DEFAULT '',  -- offsets already fired, CSV
  message     TEXT,                      -- optional extra text
  mention     TEXT,                      -- optional mention (@everyone / <@&role>)
  enabled     INTEGER NOT NULL DEFAULT 1,
  created_by  TEXT,
  created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evt_guild ON scheduled_events(guild_id);
"""

# Default lead days for event countdowns: one month/2 weeks/1 week/3 days/1 day before + the day itself.
EVENT_DEFAULT_LEADS: tuple[int, ...] = (30, 14, 7, 3, 1, 0)

_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))  # minute hour day month weekday


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
    """Invalid cron expression."""


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    """Parse one cron field into a set of allowed values. Supports `*`, `N`, `*/N`, `A-B`, `A-B/N`, comma lists."""
    out: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise CronError(t("announcements.err_empty_field", field=field))
        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError(t("announcements.err_bad_step", part=part))
            step = int(step_s)
        else:
            base = part
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, _, b = base.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise CronError(t("announcements.err_bad_range", part=part))
            start, end = int(a), int(b)
        elif base.isdigit():
            start = end = int(base)
        else:
            raise CronError(t("announcements.err_bad_value", part=part))
        if start < lo or end > hi or start > end:
            raise CronError(t("announcements.err_out_of_range", part=part, lo=lo, hi=hi))
        out.update(range(start, end + 1, step))
    return out


def parse_cron(expr: str) -> list[set[int]]:
    """Parse a 5-field cron into a list of per-field allowed-value sets (also validates). CronError on failure."""
    fields = (expr or "").split()
    if len(fields) != 5:
        raise CronError(t("announcements.err_cron_fields"))
    return [_parse_field(f, lo, hi) for f, (lo, hi) in zip(fields, _FIELD_BOUNDS)]


def cron_matches(expr: str, dt: datetime) -> bool:
    """Whether dt (a time in the relevant timezone) matches the cron. Day/weekday follow vixie-cron
    semantics (OR if either is restricted; always match if both are *)."""
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
    """KST minute-granularity key for preventing duplicate firings."""
    return datetime.fromtimestamp(now_utc, KST).strftime("%Y-%m-%d %H:%M")


def is_due(ann: Announcement, now_utc: float) -> bool:
    """Whether this announcement should fire now (pure decision)."""
    if not ann.enabled:
        return False
    key = minute_key(now_utc)
    if ann.cron:
        if ann.last_fired_minute == key:  # already fired this minute
            return False
        return cron_matches(ann.cron, datetime.fromtimestamp(now_utc, KST))
    if ann.run_at is not None:  # one-shot
        return ann.last_fired_minute is None and now_utc >= ann.run_at
    return False


@dataclass(frozen=True)
class Event:
    """A single event schedule. Countdown announcements are posted lead_days (days) ahead of event_at."""

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
    """List of lead offsets that should fire now (not-yet-fired ones whose trigger has arrived, up to 1 hour after the event)."""
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


def render_event_message(evt: Event, days: int) -> str:
    """Event countdown announcement text (discord-free). days = remaining days to display (D-DAY if ≤ 0)."""
    when = datetime.fromtimestamp(evt.event_at, KST)
    letters = t("announcements.weekday_letters")
    if len(letters) < 7:  # a short/partial locale value would IndexError; use the ko reference (>=7)
        letters = t("announcements.weekday_letters", locale=FALLBACK_LOCALE)
    dow = letters[when.weekday()]
    tag = t("announcements.dday_label") if days <= 0 else t("announcements.dcount_label", days=days)
    lines = [
        t("announcements.title_line", tag=tag, title=evt.title),
        t(
            "announcements.date_label",
            date=when.strftime("%Y-%m-%d"),
            dow=dow,
            time=when.strftime("%H:%M"),
        ),
    ]
    if evt.message:
        lines.append(evt.message)
    body = "\n".join(lines)
    if evt.mention:
        body = f"{evt.mention}\n{body}"
    return body


class AnnouncementStore:
    """Scheduled-announcement SQLite store (can reuse the same memory.db file). discord-free."""

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
            raise ValueError(t("announcements.err_cron_or_runat"))
        if cron:
            parse_cron(cron)  # validate
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
    """Event-schedule (countdown announcement) SQLite store. Reuses the same memory.db file. discord-free."""

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
        import math
        import time as _t

        now_ts = now if now is not None else _t.time()
        leads = tuple(sorted({int(x) for x in lead_days}, reverse=True))
        if any(d < 0 for d in leads):
            raise ValueError(t("announcements.err_lead_negative"))
        # Registration-time handling (key):
        #  - If the event is already over (including the 1-hour grace), skip everything and fire nothing.
        #  - On "late registration" (the first milestone has already passed), announce the current
        #    remaining days (D-cur) once immediately, skip older milestones → then only the remaining
        #    milestones in order.
        #  - If still before the first milestone, no immediate announcement — follow the scheduled milestones.
        effective = leads
        if event_at + 3600 < now_ts:
            prefired = sorted(leads, reverse=True)
        elif leads and now_ts >= event_at - max(leads) * 86400:
            remaining = max(0.0, (event_at - now_ts) / 86400.0)
            cur_days = round(remaining)  # remaining days for the label
            fire_lead = math.ceil(remaining)  # trigger ≤ now → guarantees immediate firing
            effective = tuple(sorted(set(leads) | {fire_lead}, reverse=True))
            # skip milestones ≥ cur (current/past) but keep fire_lead for immediate firing
            prefired = sorted((d for d in effective if d >= cur_days and d != fire_lead), reverse=True)
        else:
            prefired = []
        cur = self._db.execute(
            "INSERT INTO scheduled_events "
            "(guild_id, channel_id, title, event_at, lead_days, fired_leads, message, mention, "
            " enabled, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                str(guild_id),
                str(channel_id),
                title,
                float(event_at),
                ",".join(str(d) for d in effective),
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
