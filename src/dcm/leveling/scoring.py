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

# --- 신뢰-하락(penalty) 휴리스틱 상수 (코드 기본값; penalty_weight 는 음수 XP 반환) ---
FLOOD_THRESHOLD = 5  # 슬라이딩 윈도 내 메시지 수가 이 값을 '초과'하면 플러딩
MENTION_BURST_MIN = 5  # 한 메시지의 멘션 수가 이 값 이상이면 멘션 폭주
CAPS_MIN_LEN = 12  # 이 길이 이상일 때만 CAPS 판정(짧은 약어/명령어 보호)
CAPS_RATIO_THRESHOLD = 0.8  # ASCII 알파벳 중 대문자 비율 임계
PENALTY_FLOOD = -30
PENALTY_MENTION_BURST = -25
PENALTY_CAPS = -10
PENALTY_DANGER = -50
PENALTY_INJECTION = -60  # 인젝션 신호 페널티(2차; ingest 경로, 게이팅·cap·shadow 는 service)
# 위험(스캠/피싱) 콘텐츠 보수적 마커. 길드 opt-in(기본 off)·shadow 권장 — 명백한 디스코드 스캠만 본다.
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


def caps_ratio(text: str) -> float:
    """ASCII 알파벳 중 대문자 비율 [0,1]. 알파벳이 없으면 0.0(한글 등 비-casing 문자 보호)."""
    letters = [c for c in (text or "") if c.isascii() and c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def penalty_weight(text: str, flood_count: int, mention_count: int, caps_ratio: float) -> int:
    """신뢰-하락 페널티 XP (<= 0). 정상 메시지는 0. 의존성 0(LLM/DB 호출 없음).

    도배(플러딩)·멘션 폭주·과도한 대문자에 음수 페널티를 합산한다. 한글 등 대소문자가
    없는 텍스트는 caps_ratio 0.0 이라 CAPS 페널티에서 자연 제외된다.
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
    """위험(스캠/피싱) 콘텐츠 페널티 XP (<= 0). 보수적 마커 매칭만, 의존성 0(LLM/DB 없음).

    오탐을 줄이려 명백한 디스코드 스캠 문구만 본다. 길드 opt-in(기본 off)·shadow 권장.
    """
    low = (text or "").lower()
    return PENALTY_DANGER if any(m in low for m in DANGER_MARKERS) else 0


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
