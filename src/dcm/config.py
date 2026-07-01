from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, loaded from environment / `.env` (ARCHITECTURE.md §9)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Required secrets
    discord_token: str
    # One key, or several comma-separated for the key pool (ARCHITECTURE.md §9.1).
    anthropic_api_key: str

    # Identity
    bot_name: str = "썩스가재"
    # Locale for the bot's user-facing strings (ARCHITECTURE.md §10). Ships with `ko` and `en`;
    # defaults to `ko`, which preserves the bot's original language. Add a locale by dropping a
    # catalog under src/dcm/i18n/locales/ and setting this to its code.
    bot_locale: str = "ko"
    persona_file: str = "persona.md"
    knowledge_file: str = "knowledge.md"  # static knowledge about the server/study/mentors (always included in the system prompt when present)

    # Server management (slash commands) — ralplan S1 / ARCHITECTURE.md §3.
    # Discord guild id for guild-scoped slash-command registration.
    # Required: unset raises a clear boot-time validation error.
    admin_guild_id: int

    # Authz: designated admin role id (ralplan S2). Members holding this role may
    # command privileged actions (channel/role/category/moderation). Will replace the
    # jiwoo Manage-Guild check in S2. Required: unset raises a clear boot error.
    admin_role_id: int

    # Onboarding (ralplan S6) — all optional; onboarding activates only when set.
    welcome_channel_id: int | None = None  # channel to post the join welcome in
    # Join welcome text ({name} = display name). Empty → the active locale's default
    # greeting (i18n onboarding.welcome_default, ARCHITECTURE.md §10).
    welcome_message: str = ""
    default_role_id: int | None = None  # auto-assigned to new members on join

    # LLM
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 600  # response cap (ARCHITECTURE.md §14.4)
    ingest_model: str = "claude-haiku-4-5-20251001"  # cheaper model for ingestion (ARCHITECTURE.md §12)

    # Abuse limits / context
    max_input_chars: int = 4000  # input cap (ARCHITECTURE.md §14.4)
    cooldown_seconds: float = 3.0  # per-user mention cooldown (ARCHITECTURE.md §14.4)
    recent_buffer_size: int = 12
    # Monopoly-mitigation nudge style (anti-fatigue): divider (cut line, default) | thread (move to a thread) | off
    nudge_style: str = "divider"

    # Memory (M2) — ARCHITECTURE.md §5, §6
    memory_db: str = "data/memory.db"
    embedding_provider: str = "local"  # local | voyage | openai
    embedding_api_key: str = ""
    embedding_model: str = ""  # empty → provider default
    retrieval_top_n: int = 6
    w_rel: float = 0.55  # retrieval weights (ARCHITECTURE.md §5.4)
    w_rec: float = 0.20
    w_imp: float = 0.25
    half_life_base_days: float = 3.0  # forgetting curve base (ARCHITECTURE.md §5.5)
    dedup_threshold: float = 0.86  # cosine ≥ this → reinforce instead of duplicate (§5.3)
    subject_boost: float = 0.1

    # Activity leveling (G001-G004) — separate leveling.db (split from memory.db, lock-domain isolation)
    leveling_db: str = "data/leveling.db"

    # Background jobs (M3 forgetting / M4 growth) — ARCHITECTURE.md §7
    enable_background_jobs: bool = True
    prune_interval_hours: float = 24.0
    reflect_interval_hours: float = 24.0
    retention_threshold: float = 0.05  # prune when keep-score below this (§5.5)
    max_delete_ratio: float = 0.2  # safety: max fraction deletable per run
    forget_mode: str = "delete"  # delete | blur (gradual forgetting, §5.5)
    reflect_min_episodics: int = 5  # min episodics about a subject before consolidating (§5.6)

    log_level: str = "INFO"
