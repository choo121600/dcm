-- Activity leveling storage (leveling.db, separate from memory.db, WAL).
-- Every table is guild_id-scoped (per-guild isolation). Levels are not stored but derived from weighted_xp.

-- Per-member cumulative quality-weighted XP (non-decreasing). last_award_at seeds the in-memory cooldown after a restart.
CREATE TABLE IF NOT EXISTS activity_xp (
  guild_id      TEXT    NOT NULL,
  user_id       TEXT    NOT NULL,
  weighted_xp   INTEGER NOT NULL DEFAULT 0,
  last_award_at REAL    NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_xp ON activity_xp(guild_id, weighted_xp DESC);

-- Daily usage counter (trust gating). utc_day-scoped lookups + periodic prune prevent staleness.
CREATE TABLE IF NOT EXISTS daily_usage (
  guild_id TEXT    NOT NULL,
  user_id  TEXT    NOT NULL,
  utc_day  TEXT    NOT NULL,        -- 'YYYY-MM-DD' (fixed UTC-day)
  kind     TEXT    NOT NULL,        -- 'web' | 'llm'
  count    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id, utc_day, kind)
);

-- Level-to-role auto-grant mapping (secondary reward). One role per level per guild (1:N would need a separate child table).
CREATE TABLE IF NOT EXISTS level_role_rewards (
  guild_id TEXT    NOT NULL,
  level    INTEGER NOT NULL,
  role_id  INTEGER NOT NULL,
  UNIQUE (guild_id, level)
);
