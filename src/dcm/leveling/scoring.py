"""Leveling pure functions (0 dependencies, integer XP domain).

Levels are not stored but computed on read from cumulative XP (changing the curve needs no data
migration). The curve is MEE6-style: XP needed to go from level n to n+1 = 5n^2 + 50n + 100.
Operates in the integer domain to avoid floating-point boundary-comparison issues.
"""
from __future__ import annotations

import datetime
import re
from collections import Counter

# --- XP-award heuristic constants (code defaults) ---
BASE_XP = 15
SHORT_LEN = 8
SPAM_MIN_LEN = 4
SPAM_DOMINANCE = 0.70

# quality weights
W_NORMAL = 1.0
W_SHORT = 0.3
W_EMOJI_ONLY = 0.1
W_SPAM = 0.2
W_EMPTY = 0.0

# --- trust-decay (penalty) heuristic constants (code defaults; penalty_weight returns negative XP) ---
FLOOD_THRESHOLD = 5  # flooding when the message count within the sliding window 'exceeds' this value
MENTION_BURST_MIN = 5  # a mention burst when a single message has at least this many mentions
CAPS_MIN_LEN = 12  # only judge CAPS at or above this length (protects short acronyms/commands)
CAPS_RATIO_THRESHOLD = 0.8  # threshold uppercase ratio among ASCII letters
PENALTY_FLOOD = -30
PENALTY_MENTION_BURST = -25
PENALTY_CAPS = -10
PENALTY_DANGER = -50
PENALTY_INJECTION = -60  # injection-signal penalty (secondary; ingest path, gating/cap/shadow handled in service)
# conservative markers for dangerous (scam/phishing) content. Guild opt-in (off by default), shadow recommended — only obvious Discord scams are matched.
DANGER_MARKERS = (
    "free nitro",
    "free discord nitro",
    "nitro free",
    "steamcommunity.com/gift",
    "discordgift",
    "discord-gift",
    "dlscord",
    "ip grabber",
    "grabify.link",
    "무료 니트로",
    "공짜 니트로",
)

_WORD_RE = re.compile(r"[^\W_]", re.UNICODE)  # one or more alphanumeric/Unicode word characters
_WS_RE = re.compile(r"\s+")


def quality_weight(text: str) -> float:
    """Message 'quality' weight [0,1] — judged from length/composition alone, no LLM call.

    Priority: empty string → spam (single-character dominance) → emoji/punctuation only → short → normal.
    """
    s = (text or "").strip()
    if not s:
        return W_EMPTY
    compact = _WS_RE.sub("", s)
    if len(compact) >= SPAM_MIN_LEN:
        most = max(Counter(compact).values())
        if most / len(compact) > SPAM_DOMINANCE:
            return W_SPAM  # spam (e.g. "ㅋㅋㅋㅋㅋ", "aaaaaa")
    if not _WORD_RE.search(s):
        return W_EMOJI_ONLY  # emoji/punctuation only (0 word characters)
    if len(s) < SHORT_LEN:
        return W_SHORT  # too-short one-liner
    return W_NORMAL


def xp_award(text: str, base_xp: int = BASE_XP) -> int:
    """Integer XP to award for this message. Always in the integer domain."""
    return int(round(base_xp * quality_weight(text)))


def caps_ratio(text: str) -> float:
    """Uppercase ratio among ASCII letters [0,1]. 0.0 when there are no letters (protects non-casing characters such as Hangul)."""
    letters = [c for c in (text or "") if c.isascii() and c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def penalty_weight(text: str, flood_count: int, mention_count: int, caps_ratio: float) -> int:
    """Trust-decay penalty XP (<= 0). 0 for a normal message. 0 dependencies (no LLM/DB calls).

    Sums negative penalties for spam (flooding), mention bursts, and excessive uppercase. Text
    without case such as Hangul has caps_ratio 0.0 and is thus naturally excluded from the CAPS
    penalty.
    """
    penalty = 0
    if int(flood_count) > FLOOD_THRESHOLD:
        penalty += PENALTY_FLOOD
    if int(mention_count) >= MENTION_BURST_MIN:
        penalty += PENALTY_MENTION_BURST
    s = (text or "").strip()
    if len(s) >= CAPS_MIN_LEN and float(caps_ratio) >= CAPS_RATIO_THRESHOLD:
        penalty += PENALTY_CAPS
    return penalty


def danger_score(text: str) -> int:
    """Dangerous (scam/phishing) content penalty XP (<= 0). Conservative marker matching only,
    0 dependencies (no LLM/DB).

    Matches only obvious Discord scam phrases to reduce false positives. Guild opt-in (off by
    default), shadow recommended.
    """
    low = (text or "").lower()
    return PENALTY_DANGER if any(m in low for m in DANGER_MARKERS) else 0


def level_step(level: int) -> int:
    """XP needed to go from level `level` to `level+1` (MEE6-style curve)."""
    n = max(0, int(level))
    return 5 * n * n + 50 * n + 100


def cum_cost(level: int) -> int:
    """Cumulative total XP needed to 'reach' level `level`. cum_cost(0) = 0."""
    lvl = max(0, int(level))
    total = 0
    for k in range(lvl):
        total += level_step(k)
    return total


def level(total_xp: int) -> int:
    """Highest level reachable with cumulative XP (0 XP = level 0). Incremental accumulation, O(n)."""
    xp = max(0, int(total_xp))
    n = 0
    acc = 0
    while True:
        step = level_step(n)
        if acc + step > xp:
            return n
        acc += step
        n += 1


def progress(total_xp: int) -> float:
    """Progress from the current level toward the next [0,1)."""
    xp = max(0, int(total_xp))
    lvl = level(xp)
    floor = cum_cost(lvl)
    step = level_step(lvl)
    if step <= 0:
        return 0.0
    return (xp - floor) / step


def xp_to_next(total_xp: int) -> int:
    """XP remaining to the next level (integer)."""
    xp = max(0, int(total_xp))
    lvl = level(xp)
    return cum_cost(lvl + 1) - xp


def utc_day(epoch: float) -> str:
    """Fixed UTC-day boundary key 'YYYY-MM-DD' (a pure function given epoch)."""
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%d")
