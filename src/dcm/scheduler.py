from __future__ import annotations

import asyncio
import logging
import time

from .embeddings import Embedder
from .llm import LLMClient
from .memory.forgetting import run_pruning
from .memory.reflection import run_reflection
from .memory.store import MemoryStore

log = logging.getLogger(__name__)


class BackgroundJobs:
    """Periodic memory maintenance: pruning (forget) and reflection (grow) (DESIGN.md §7).

    Dependency-free asyncio loops rather than apscheduler. Each loop sleeps first, so a fresh
    process doesn't immediately churn an empty store.
    """

    def __init__(
        self,
        store: MemoryStore,
        llm: LLMClient,
        embedder: Embedder,
        *,
        prune_interval_hours: float,
        reflect_interval_hours: float,
        retention_threshold: float,
        max_delete_ratio: float,
        half_life_base_days: float,
        forget_mode: str,
        reflect_min_episodics: int,
        ingest_model: str,
        leveling_store=None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._embedder = embedder
        self._prune_interval = prune_interval_hours * 3600
        self._reflect_interval = reflect_interval_hours * 3600
        self._retention_threshold = retention_threshold
        self._max_delete_ratio = max_delete_ratio
        self._half_life_base_days = half_life_base_days
        self._forget_mode = forget_mode
        self._reflect_min_episodics = reflect_min_episodics
        self._ingest_model = ingest_model
        self._leveling_store = leveling_store
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._loop("pruning", self._prune_interval, self._prune)),
            asyncio.create_task(self._loop("reflection", self._reflect_interval, self._reflect)),
        ]
        if self._leveling_store is not None:
            self._tasks.append(
                asyncio.create_task(
                    self._loop("daily-usage-prune", 86400, self._prune_daily_usage)
                )
            )
        log.info("background jobs started (prune/%.0fh, reflect/%.0fh)",
                 self._prune_interval / 3600, self._reflect_interval / 3600)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    async def _loop(self, name: str, interval: float, job) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                await job()
                log.info("memory stats after %s: %s", name, self._store.stats())
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("%s job crashed; continuing", name)

    async def _prune_daily_usage(self) -> None:
        # 신뢰 게이팅(G003): 현재 UTC-day 미만의 daily_usage 행을 일일 정리(stale 위생).
        from .leveling.scoring import utc_day

        self._leveling_store.prune_daily_usage(utc_day(time.time()))

    async def _prune(self) -> None:
        # 멀티길드(P5): 길드별 핸들로만 가지치기 — 교차길드 미접근.
        for gid in self._store.guild_ids():
            await run_pruning(
                self._store.for_guild(gid),
                now=time.time(),
                threshold=self._retention_threshold,
                max_delete_ratio=self._max_delete_ratio,
                half_life_base_days=self._half_life_base_days,
                mode=self._forget_mode,
                llm=self._llm,
                embedder=self._embedder,
                blur_model=self._ingest_model,
            )

    async def _reflect(self) -> None:
        # 멀티길드(P5): 길드별 핸들로만 reflection — 통합 semantic 의 source_ids 가 단일 길드로 한정.
        for gid in self._store.guild_ids():
            await run_reflection(
                self._llm,
                self._store.for_guild(gid),
                self._embedder,
                now=time.time(),
                min_episodics=self._reflect_min_episodics,
                model=self._ingest_model,
            )
