"""LevelingStore — the single entry point to leveling.db (separate from memory.db, WAL).

Every DB op is serialized through SqliteWriter (a single dedicated thread). Writes are
fire-and-forget (non-blocking on the hot path); reads are FIFO-serialized through the same queue
and block briefly (guaranteeing read-after-write consistency). Every method is guild_id-scoped
(per-guild isolation).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .writer import SqliteWriter

_SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
_PRAGMAS = ("PRAGMA journal_mode=WAL", "PRAGMA busy_timeout=5000")


class LevelingStore:
    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = SqliteWriter(str(path), schema=_SCHEMA, pragmas=_PRAGMAS)
        self._writer.start()

    # --- writes (fire-and-forget, serialized) ---

    def add_xp(self, guild_id: int | str, user_id: int | str, xp: int, now: float) -> None:
        """Award quality-weighted XP. xp may be negative (a trust-decay penalty) — clamped to a floor of 0 (two-statement pattern)."""
        gid, uid, amount, ts = str(guild_id), str(user_id), int(xp), float(now)

        def op(conn: sqlite3.Connection) -> None:
            # Two-statement atomic pattern (run consecutively within a single writer op): seed the
            # row → clamp with MAX(0, ...). A negative amount (penalty) is still floored at 0 — even
            # a first negative on a new row is seeded to 0 then clamped.
            conn.execute(
                "INSERT OR IGNORE INTO activity_xp (guild_id, user_id, weighted_xp, last_award_at) "
                "VALUES (?, ?, 0, ?)",
                (gid, uid, ts),
            )
            conn.execute(
                "UPDATE activity_xp SET weighted_xp = MAX(0, weighted_xp + ?), last_award_at = ? "
                "WHERE guild_id = ? AND user_id = ?",
                (amount, ts, gid, uid),
            )
            conn.commit()

        self._writer.submit(op, wait=False)

    def incr_daily_usage(
        self,
        guild_id: int | str,
        user_id: int | str,
        utc_day: str,
        kind: str,
        amount: int = 1,
    ) -> None:
        gid, uid, day, k, amt = (
            str(guild_id),
            str(user_id),
            str(utc_day),
            str(kind),
            int(amount),
        )

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO daily_usage (guild_id, user_id, utc_day, kind, count) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id, utc_day, kind) DO UPDATE SET "
                "count = count + excluded.count",
                (gid, uid, day, k, amt),
            )
            conn.commit()

        self._writer.submit(op, wait=False)

    def prune_daily_usage(self, older_than_day: str) -> None:
        cutoff = str(older_than_day)

        def op(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM daily_usage WHERE utc_day < ?", (cutoff,))
            conn.commit()

        self._writer.submit(op, wait=False)

    def set_role_reward(self, guild_id: int | str, level: int, role_id: int) -> None:
        gid, lvl, rid = str(guild_id), int(level), int(role_id)

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO level_role_rewards (guild_id, level, role_id) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, level) DO UPDATE SET role_id = excluded.role_id",
                (gid, lvl, rid),
            )
            conn.commit()

        self._writer.submit(op, wait=False)

    def remove_role_reward(self, guild_id: int | str, level: int) -> None:
        gid, lvl = str(guild_id), int(level)

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM level_role_rewards WHERE guild_id = ? AND level = ?", (gid, lvl)
            )
            conn.commit()

        self._writer.submit(op, wait=False)

    # --- reads (serialized, block briefly) ---

    def get_record(self, guild_id: int | str, user_id: int | str) -> tuple[int, float]:
        gid, uid = str(guild_id), str(user_id)

        def op(conn: sqlite3.Connection):
            return conn.execute(
                "SELECT weighted_xp, last_award_at FROM activity_xp "
                "WHERE guild_id = ? AND user_id = ?",
                (gid, uid),
            ).fetchone()

        row = self._writer.submit(op, wait=True)
        if row is None:
            return (0, 0.0)
        return (int(row["weighted_xp"]), float(row["last_award_at"]))

    def leaderboard(self, guild_id: int | str, top_n: int = 10) -> list[tuple[str, int]]:
        gid, limit = str(guild_id), int(top_n)

        def op(conn: sqlite3.Connection):
            return conn.execute(
                "SELECT user_id, weighted_xp FROM activity_xp WHERE guild_id = ? "
                "ORDER BY weighted_xp DESC, user_id ASC LIMIT ?",
                (gid, limit),
            ).fetchall()

        rows = self._writer.submit(op, wait=True)
        return [(r["user_id"], int(r["weighted_xp"])) for r in rows]

    def get_daily_usage(
        self, guild_id: int | str, user_id: int | str, utc_day: str, kind: str
    ) -> int:
        gid, uid, day, k = str(guild_id), str(user_id), str(utc_day), str(kind)

        def op(conn: sqlite3.Connection):
            return conn.execute(
                "SELECT count FROM daily_usage "
                "WHERE guild_id = ? AND user_id = ? AND utc_day = ? AND kind = ?",
                (gid, uid, day, k),
            ).fetchone()

        row = self._writer.submit(op, wait=True)
        return int(row["count"]) if row else 0

    def get_role_rewards(self, guild_id: int | str) -> list[tuple[int, int]]:
        gid = str(guild_id)

        def op(conn: sqlite3.Connection):
            return conn.execute(
                "SELECT level, role_id FROM level_role_rewards WHERE guild_id = ? "
                "ORDER BY level ASC",
                (gid,),
            ).fetchall()

        rows = self._writer.submit(op, wait=True)
        return [(int(r["level"]), int(r["role_id"])) for r in rows]

    def close(self) -> None:
        """Graceful shutdown (drain the queue, then close the connection) — R2."""
        self._writer.stop()
