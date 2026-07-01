"""Per-server (guild) settings store (multi-guild v2).

discord-free pure module. Reuses the same SQLite file as memory.db (single backup/migration).
env values like admin_guild_id/admin_role_id are seeded once only as defaults for the existing
seed guild; all other guilds are left unset (= triggers the authz fallback).
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
  guild_id                  TEXT PRIMARY KEY,
  admin_role_id             INTEGER,
  welcome_channel_id        INTEGER,
  default_role_id           INTEGER,
  welcome_message           TEXT,
  leveling_enabled          INTEGER,
  leveling_cooldown_seconds REAL,
  leveling_top_n            INTEGER,
  leveling_decay_enabled    INTEGER,
  leveling_decay_shadow     INTEGER,
  leveling_danger_enabled   INTEGER,
  leveling_injection_enabled INTEGER,
  updated_at                REAL
);
"""

# Columns _upsert may inline directly into SQL (not external input — injection-prevention allowlist).
_SETTABLE = frozenset(
    {
        "admin_role_id",
        "welcome_channel_id",
        "default_role_id",
        "welcome_message",
        "leveling_enabled",
        "leveling_cooldown_seconds",
        "leveling_top_n",
        "leveling_decay_enabled",
        "leveling_decay_shadow",
        "leveling_danger_enabled",
        "leveling_injection_enabled",
    }
)


@dataclass(frozen=True)
class GuildSettings:
    guild_id: str
    admin_role_id: int = 0  # 0 = unset → authz uses the Discord-permission fallback
    welcome_channel_id: int | None = None
    default_role_id: int | None = None
    welcome_message: str | None = None
    leveling_enabled: bool | None = None  # None = enabled by default (uses the service default)
    leveling_cooldown_seconds: float | None = None  # None = service default (60s)
    leveling_top_n: int | None = None  # None = service default (10)
    leveling_decay_enabled: bool | None = None  # None = OFF by default (trust-decay disabled)
    leveling_decay_shadow: bool | None = None  # None = enforce by default (actually deducts when decay is enabled)
    leveling_danger_enabled: bool | None = None  # None = OFF by default (danger wordlist disabled)
    leveling_injection_enabled: bool | None = None  # None = OFF by default (injection signal disabled)


class GuildSettingsStore:
    def __init__(self, db_path: str, *, seed: GuildSettings | None = None) -> None:
        path = Path(db_path)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._migrate()
        if seed is not None:
            self._seed(seed)

    def _migrate(self) -> None:
        # Add leveling columns if an existing DB lacks them (idempotent). CREATE TABLE IF NOT EXISTS
        # does not alter existing-table columns, so we ALTER here (same pattern as memory.store._migrate).
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(guild_settings)")}
        for col, ddl in (
            ("leveling_enabled", "INTEGER"),
            ("leveling_cooldown_seconds", "REAL"),
            ("leveling_top_n", "INTEGER"),
            ("leveling_decay_enabled", "INTEGER"),
            ("leveling_decay_shadow", "INTEGER"),
            ("leveling_danger_enabled", "INTEGER"),
            ("leveling_injection_enabled", "INTEGER"),
        ):
            if col not in cols:
                self._db.execute(f"ALTER TABLE guild_settings ADD COLUMN {col} {ddl}")
        self._db.commit()

    def _seed(self, seed: GuildSettings) -> None:
        # Seed-guild defaults once only (values the operator later changed are not overwritten — INSERT OR IGNORE).
        self._db.execute(
            "INSERT OR IGNORE INTO guild_settings (guild_id, admin_role_id, welcome_channel_id, "
            "default_role_id, welcome_message, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(seed.guild_id),
                int(seed.admin_role_id or 0),
                seed.welcome_channel_id,
                seed.default_role_id,
                seed.welcome_message,
                time.time(),
            ),
        )
        self._db.commit()

    def get(self, guild_id: int | str) -> GuildSettings:
        row = self._db.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (str(guild_id),)
        ).fetchone()
        if row is None:
            return GuildSettings(guild_id=str(guild_id))
        return GuildSettings(
            guild_id=row["guild_id"],
            admin_role_id=int(row["admin_role_id"] or 0),
            welcome_channel_id=row["welcome_channel_id"],
            default_role_id=row["default_role_id"],
            welcome_message=row["welcome_message"],
            leveling_enabled=(
                None if row["leveling_enabled"] is None else bool(row["leveling_enabled"])
            ),
            leveling_cooldown_seconds=row["leveling_cooldown_seconds"],
            leveling_top_n=row["leveling_top_n"],
            leveling_decay_enabled=(
                None if row["leveling_decay_enabled"] is None else bool(row["leveling_decay_enabled"])
            ),
            leveling_decay_shadow=(
                None if row["leveling_decay_shadow"] is None else bool(row["leveling_decay_shadow"])
            ),
            leveling_danger_enabled=(
                None if row["leveling_danger_enabled"] is None else bool(row["leveling_danger_enabled"])
            ),
            leveling_injection_enabled=(
                None
                if row["leveling_injection_enabled"] is None
                else bool(row["leveling_injection_enabled"])
            ),
        )

    def _upsert(self, guild_id: int | str, field: str, value) -> None:
        if field not in _SETTABLE:  # defensive: only called by the fixed set_* methods, but guard once more
            raise ValueError(f"not a settable field: {field}")
        self._db.execute(
            f"INSERT INTO guild_settings (guild_id, {field}, updated_at) VALUES (?, ?, ?) "
            f"ON CONFLICT(guild_id) DO UPDATE SET {field} = excluded.{field}, "
            "updated_at = excluded.updated_at",
            (str(guild_id), value, time.time()),
        )
        self._db.commit()

    def set_admin_role(self, guild_id: int | str, role_id: int) -> None:
        self._upsert(guild_id, "admin_role_id", int(role_id))

    def set_welcome_channel(self, guild_id: int | str, channel_id: int) -> None:
        self._upsert(guild_id, "welcome_channel_id", int(channel_id))

    def set_default_role(self, guild_id: int | str, role_id: int) -> None:
        self._upsert(guild_id, "default_role_id", int(role_id))

    def set_welcome_message(self, guild_id: int | str, message: str) -> None:
        self._upsert(guild_id, "welcome_message", str(message))

    def set_leveling_enabled(self, guild_id: int | str, enabled: bool) -> None:
        self._upsert(guild_id, "leveling_enabled", 1 if enabled else 0)

    def set_leveling_cooldown_seconds(self, guild_id: int | str, seconds: float) -> None:
        self._upsert(guild_id, "leveling_cooldown_seconds", float(seconds))

    def set_leveling_top_n(self, guild_id: int | str, top_n: int) -> None:
        self._upsert(guild_id, "leveling_top_n", int(top_n))

    def set_leveling_decay_enabled(self, guild_id: int | str, enabled: bool) -> None:
        self._upsert(guild_id, "leveling_decay_enabled", 1 if enabled else 0)

    def set_leveling_decay_shadow(self, guild_id: int | str, shadow: bool) -> None:
        self._upsert(guild_id, "leveling_decay_shadow", 1 if shadow else 0)

    def set_leveling_danger_enabled(self, guild_id: int | str, enabled: bool) -> None:
        self._upsert(guild_id, "leveling_danger_enabled", 1 if enabled else 0)

    def set_leveling_injection_enabled(self, guild_id: int | str, enabled: bool) -> None:
        self._upsert(guild_id, "leveling_injection_enabled", 1 if enabled else 0)

    def close(self) -> None:
        self._db.close()
