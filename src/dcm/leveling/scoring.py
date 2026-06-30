"""레벨링 순수함수 (의존성 0, 정수 XP 도메인).

레벨은 저장하지 않고 누적 XP 에서 조회 시 산정한다(곡선 변경이 데이터 마이그레이션 불필요).
곡선은 MEE6식: 레벨 n→n+1 로 가는 데 필요한 XP = 5n^2 + 50n + 100.
정수 도메인으로 운용해 부동소수 경계비교 이슈를 회피한다.
"""
from __future__ import annotations

import datetime
import re
from collections import Counter

# --- XP 적립 휴리스틱 상수 (코드 기본값) ---
BASE_XP = 15
SHORT_LEN = 8
SPAM_MIN_LEN = 4
SPAM_DOMINANCE = 0.70

# 질 가중치
W_NORMAL = 1.0
W_SHORT = 0.3
W_EMOJI_ONLY = 0.1
W_SPAM = 0.2
W_EMPTY = 0.0

_WORD_RE = re.compile(r"[^\W_]", re.UNICODE)  # 알파벳/숫자/유니코드 단어문자 1개 이상
_WS_RE = re.compile(r"\s+")


def quality_weight(text: str) -> float:
    """메시지 '질' 가중치 [0,1] — LLM 호출 없이 길이/구성만으로 판정.

    우선순위: 빈 문자열 → 도배(단일문자 지배) → 이모지/부호 only → 짧음 → 정상.
    """
    s = (text or "").strip()
    if not s:
        return W_EMPTY
    compact = _WS_RE.sub("", s)
    if len(compact) >= SPAM_MIN_LEN:
        most = max(Counter(compact).values())
        if most / len(compact) > SPAM_DOMINANCE:
            return W_SPAM  # 도배 (예: "ㅋㅋㅋㅋㅋ", "aaaaaa")
    if not _WORD_RE.search(s):
        return W_EMOJI_ONLY  # 이모지/문장부호 only (단어문자 0)
    if len(s) < SHORT_LEN:
        return W_SHORT  # 너무 짧은 단답
    return W_NORMAL


def xp_award(text: str, base_xp: int = BASE_XP) -> int:
    """이 메시지로 적립할 정수 XP. 항상 정수 도메인."""
    return int(round(base_xp * quality_weight(text)))


def level_step(level: int) -> int:
    """레벨 `level` → `level+1` 로 가는 데 필요한 XP (MEE6식 곡선)."""
    n = max(0, int(level))
    return 5 * n * n + 50 * n + 100


def cum_cost(level: int) -> int:
    """레벨 `level` 에 '도달' 하기 위한 누적 총 XP. cum_cost(0) = 0."""
    lvl = max(0, int(level))
    total = 0
    for k in range(lvl):
        total += level_step(k)
    return total


def level(total_xp: int) -> int:
    """누적 XP 로 도달 가능한 최고 레벨 (0 XP = 레벨 0). 증분 누적 O(n)."""
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
    """현재 레벨에서 다음 레벨까지 진행도 [0,1)."""
    xp = max(0, int(total_xp))
    lvl = level(xp)
    floor = cum_cost(lvl)
    step = level_step(lvl)
    if step <= 0:
        return 0.0
    return (xp - floor) / step


def xp_to_next(total_xp: int) -> int:
    """다음 레벨까지 남은 XP (정수)."""
    xp = max(0, int(total_xp))
    lvl = level(xp)
    return cum_cost(lvl + 1) - xp


def utc_day(epoch: float) -> str:
    """고정 UTC-day 경계 키 'YYYY-MM-DD' (epoch 주어지면 순수함수)."""
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%d")
