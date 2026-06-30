"""LevelingStore — leveling.db (memory.db 와 분리, WAL) 의 단일 진입점.

모든 DB op 는 SqliteWriter(단일 전용 스레드)로 직렬화된다. 쓰기는 fire-and-forget(핫패스
비블로킹), 읽기는 같은 큐로 FIFO 직렬화돼 잠깐 블록한다(쓰기 후 읽기 일관성 보장).
모든 메서드는 guild_id 스코프(길드별 격리).
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

    # --- writes (fire-and-forget, 직렬화) ---

    def add_xp(self, guild_id: int | str, user_id: int | str, xp: int, now: float) -> None:
        """질-가중 XP 적립. xp 는 음수(신뢰-하락 페널티) 가능 — 하한0 클램프(2문 패턴)."""
        gid, uid, amount, ts = str(guild_id), str(user_id), int(xp), float(now)

        def op(conn: sqlite3.Connection) -> None:
            # 2문 원자 패턴(단일 writer op 안에서 연속 실행): 행 시드 → MAX(0, ...) 클램프.
            # 음수 amount(페널티)도 하한0 보장 — 신규행 첫 음수도 0 으로 시드 후 클램프된다.
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

    # --- reads (직렬화, 잠깐 블록) ---

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
        """그레이스풀 종료(큐 drain 후 connection close) — R2."""
        self._writer.stop()
