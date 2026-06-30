from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .config import Settings
from .embeddings import build_embedder
from .llm import LLMClient, parse_credentials
from .logging_setup import setup_logging
from .memory.ingest import IngestionPipeline
from .memory.store import MemoryStore
from .orchestrator import Orchestrator
from .platform.pycord_adapter import PycordAdapter
from .service.guild_admin import GuildAdminService
from .agent.router import NLRouter
from .scheduler import BackgroundJobs
from .service.onboarding import OnboardingPolicy
from .service.guild_settings import GuildSettings, GuildSettingsStore

log = logging.getLogger("dcm")

# Project root = two levels up from this file (src/dcm/main.py).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve_persona(persona_file: str) -> Path:
    path = Path(persona_file)
    return path if path.is_absolute() else _PROJECT_ROOT / path


async def _run() -> None:
    settings = Settings()  # loads from environment / .env
    setup_logging(settings.log_level)

    creds = parse_credentials(settings.anthropic_api_key)
    log.info("loaded %d API credential(s)", len(creds))  # count only, never the key value

    llm = LLMClient(creds, model=settings.model, max_tokens=settings.max_tokens)

    # Memory engine (M2) — DESIGN.md §5, §6.
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

    # 서버별 설정 저장소 (멀티길드 v2) — memory.db 동일 파일, 기존 길드 기본값만 시드.
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
    )

    # Guild-management slash commands (ralplan S2).
    admin_service = GuildAdminService(adapter, adapter.pending)
    adapter.register_admin_commands(admin_service)

    # NL 라우터 (ralplan S3): 자연어 관리 명령을 닫힌 동사셋으로 라우팅.
    nl_router = NLRouter(
        llm=llm,
        service=admin_service,
    )

    orchestrator = Orchestrator(
        llm=llm,
        persona_path=_resolve_persona(settings.persona_file),
        bot_name=settings.bot_name,
        max_input_chars=settings.max_input_chars,
        store=store,
        ingest=ingest,
        embedder=embedder,
        retrieval_top_n=settings.retrieval_top_n,
        router=nl_router,
    )
    adapter.on_mention(orchestrator.handle)

    # Periodic memory maintenance: forgetting (M3) + reflection/growth (M4) — DESIGN.md §7.
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
        )
        jobs.start()

    log.info("starting %s…", settings.bot_name)
    await adapter.run()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
