from __future__ import annotations

import math

_SECONDS_PER_DAY = 86_400.0


def half_life_seconds(importance: float, base_days: float) -> float:
    """Half-life grows with importance: important memories fade slowly (DESIGN.md §5.5)."""
    return base_days * _SECONDS_PER_DAY * max(1.0, importance)


def recency(now: float, last_access_at: float, half_life: float) -> float:
    """Exponential decay → 0.5 after one half-life. Returns a value in (0, 1]."""
    dt = max(0.0, now - last_access_at)
    if half_life <= 0:
        return 0.0
    return math.exp(-math.log(2) * dt / half_life)


def retention(importance: float, recency_value: float, access_count: int) -> float:
    """Overall keep-score used by pruning (DESIGN.md §5.5). Higher = keep."""
    importance_norm = importance / 10.0
    return importance_norm * recency_value * (1.0 + math.log(1.0 + access_count))


def retrieval_score(
    relevance: float,
    recency_value: float,
    importance: float,
    w_rel: float,
    w_rec: float,
    w_imp: float,
) -> float:
    """Weighted sum of relevance + recency + importance (DESIGN.md §5.4)."""
    importance_norm = importance / 10.0
    return w_rel * relevance + w_rec * recency_value + w_imp * importance_norm
