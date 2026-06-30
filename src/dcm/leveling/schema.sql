-- 활동 레벨링 저장 (memory.db 와 분리된 leveling.db, WAL).
-- 모든 테이블 guild_id 스코프(길드별 격리). 레벨은 저장하지 않고 weighted_xp 에서 파생.

-- 멤버별 누적 질-가중 XP (비감소). last_award_at 은 재시작 후 인메모리 쿨다운 시드용.
CREATE TABLE IF NOT EXISTS activity_xp (
  guild_id      TEXT    NOT NULL,
  user_id       TEXT    NOT NULL,
  weighted_xp   INTEGER NOT NULL DEFAULT 0,
  last_award_at REAL    NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_xp ON activity_xp(guild_id, weighted_xp DESC);

-- 일일 사용량 카운터 (신뢰 게이팅). utc_day 한정 조회 + 주기 prune 로 stale 방지.
CREATE TABLE IF NOT EXISTS daily_usage (
  guild_id TEXT    NOT NULL,
  user_id  TEXT    NOT NULL,
  utc_day  TEXT    NOT NULL,        -- 'YYYY-MM-DD' (고정 UTC-day)
  kind     TEXT    NOT NULL,        -- 'web' | 'llm'
  count    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id, utc_day, kind)
);

-- 레벨→역할 자동부여 매핑(보조 보상). 길드당 레벨별 1개 역할(1:N 은 별도 자식 테이블).
CREATE TABLE IF NOT EXISTS level_role_rewards (
  guild_id TEXT    NOT NULL,
  level    INTEGER NOT NULL,
  role_id  INTEGER NOT NULL,
  UNIQUE (guild_id, level)
);
