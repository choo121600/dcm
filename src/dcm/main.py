from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from . import i18n
from .agent.router import NLRouter
from .config import Settings
from .embeddings import build_embedder
from .leveling.service import LevelingService
from .leveling.store import LevelingStore
from .llm import LLMClient, parse_credentials
from .logging_setup import setup_logging
from .memory.ingest import IngestionPipeline
from .memory.store import MemoryStore
from .orchestrator import Orchestrator
from .platform.pycord_adapter import PycordAdapter
from .scheduler import BackgroundJobs
from .service.announcements import AnnouncementStore, EventStore
from .service.guild_admin import GuildAdminService
from .service.guild_settings import GuildSettings, GuildSettingsStore
from .service.onboarding import OnboardingPolicy
from .service.study_lookup import StudyLookup

log = logging.getLogger("dcm")

# Project root = two levels up from this file (src/dcm/main.py).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve_persona(persona_file: str) -> Path:
    path = Path(persona_file)
    return path if path.is_absolute() else _PROJECT_ROOT / path


async def _run() -> None:
    settings = Settings()  # loads from environment / .env
    setup_logging(settings.log_level)
    i18n.set_locale(settings.bot_locale)  # select the bot's user-facing language (ARCHITECTURE.md §10)
    log.info("locale: %s", i18n.get_locale())

    creds = parse_credentials(settings.anthropic_api_key)
    log.info("loaded %d API credential(s)", len(creds))  # count only, never the key value

    llm = LLMClient(creds, model=settings.model, max_tokens=settings.max_tokens)

    # Memory engine (M2) — ARCHITECTURE.md §5, §6.
    embedder = build_embedder(
        settings.embedding_provider, settings.embedding_api_key, settings.embedding_model
    )
    store = MemoryStore(
        settings.memory_db,
        weights=(settings.w_rel, settings.w_rec, settings.w_imp),
        half_life_base_days=settings.half_life_base_days,
        subject_boost=settings.subject_boost,
        seed_guild_id=str(settings.admin_guild_id),
    )
    log.info("memory store ready (%d memories)", store.count())
    ingest = IngestionPipeline(
        llm,
        store,
        embedder,
        ingest_model=settings.ingest_model,
        dedup_threshold=settings.dedup_threshold,
    )

    # Per-server settings store (multi-guild v2) — same memory.db file, seeds only the existing guild's defaults.
    guild_settings = GuildSettingsStore(
        settings.memory_db,
        seed=GuildSettings(
            guild_id=str(settings.admin_guild_id),
            admin_role_id=settings.admin_role_id,
            welcome_channel_id=settings.welcome_channel_id,
            default_role_id=settings.default_role_id,
            welcome_message=settings.welcome_message,
        ),
    )

    # Scheduled announcement store (admin bot: recurring/one-off announcements). Same memory.db file.
    announcements = AnnouncementStore(settings.memory_db)

    # Event countdown announcement store (auto D-30/14/7/3/1/DDAY). Same memory.db file.
    events = EventStore(settings.memory_db)

    onboarding = OnboardingPolicy(
        settings=guild_settings,
        welcome_channel_id=settings.welcome_channel_id,
        welcome_message=settings.welcome_message,
        default_role_id=settings.default_role_id,
    )

    adapter = PycordAdapter(
        token=settings.discord_token,
        bot_name=settings.bot_name,
        buffer_size=settings.recent_buffer_size,
        cooldown_seconds=settings.cooldown_seconds,
        guild_id=settings.admin_guild_id,
        admin_role_id=settings.admin_role_id,
        onboarding_policy=onboarding,
        guild_settings=guild_settings,
        announcements=announcements,
        events=events,
        llm=llm,
        nudge_style=settings.nudge_style,
    )

    # Guild-management slash commands (ralplan S2).
    admin_service = GuildAdminService(adapter, adapter.pending)
    adapter.register_admin_commands(admin_service)

    # Activity leveling (G001-G004): separate leveling.db + a single dedicated writer (R1). Per-guild config via guild_settings.
    leveling_store = LevelingStore(settings.leveling_db)
    leveling_service = LevelingService(leveling_store, guild_settings)
    adapter.register_leveling_commands(leveling_service)

    # NL router (ralplan S3): routes natural-language admin commands onto a closed verb set.
    nl_router = NLRouter(
        llm=llm,
        service=admin_service,
        dispatch_model=settings.ingest_model,  # classify with the cheap haiku (per-mention path, cost saving)
    )

    orchestrator = Orchestrator(
        llm=llm,
        persona_path=_resolve_persona(settings.persona_file),
        knowledge_path=_resolve_persona(settings.knowledge_file),
        bot_name=settings.bot_name,
        max_input_chars=settings.max_input_chars,
        store=store,
        ingest=ingest,
        embedder=embedder,
        retrieval_top_n=settings.retrieval_top_n,
        router=nl_router,
        leveling=leveling_service,
        studies=StudyLookup(),  # on-demand study detail reads (fetch the doc only for deep questions)
    )
    adapter.on_mention(orchestrator.handle)

    # Periodic memory maintenance: forgetting (M3) + reflection/growth (M4) — ARCHITECTURE.md §7.
    if settings.enable_background_jobs:
        jobs = BackgroundJobs(
            store,
            llm,
            embedder,
            prune_interval_hours=settings.prune_interval_hours,
            reflect_interval_hours=settings.reflect_interval_hours,
            retention_threshold=settings.retention_threshold,
            max_delete_ratio=settings.max_delete_ratio,
            half_life_base_days=settings.half_life_base_days,
            forget_mode=settings.forget_mode,
            reflect_min_episodics=settings.reflect_min_episodics,
            ingest_model=settings.ingest_model,
            leveling_store=leveling_store,
        )
        jobs.start()

    log.info("starting %s…", settings.bot_name)
    try:
        await adapter.run()
    finally:
        # R2: graceful writer shutdown (drain the queue, then close). daily_usage loss = bounded fail-open.
        leveling_store.close()
        announcements.close()
        events.close()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
