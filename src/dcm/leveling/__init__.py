"""Activity leveling system.

Awards cumulative XP by quality-weighting members' text messages with LLM-free heuristics;
levels are not stored but computed on read by a pure function. The primary reward is using the
level as a 'trust tier' to differentiate daily limits for web search and LLM conversation, with
level-to-role auto-grant as a secondary reward.

Storage is serialized into leveling.db (WAL), separate from memory.db, by a single
dedicated-thread writer.
"""
from __future__ import annotations
