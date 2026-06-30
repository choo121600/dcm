"""비활성 채널 아카이브 + 고아 역할 삭제 '계획' (discord-free, 순수 데이터).

2단계 수명주기:
  1) 아카이브: 비활성 텍스트 채널을 '📦 아카이브' 카테고리로 이동(+멤버에게 숨김). 되돌릴 수 있음.
  2) 퍼지  : '📦 아카이브' 안의 모든 채널을 삭제 + 고아 역할 삭제. 되돌릴 수 없음.
어댑터가 넘긴 채널/역할 dict 위에서 *무엇을* 할지만 결정하고, 실제 디스코드 호출(이동/생성/
삭제/권한)은 GuildAdminService 가 한다. 순수 함수라 단위 테스트가 쉽다.

설계 결정(사용자 합의):
- 비활성 기준 = 마지막 메시지 경과일 ≥ N일(기본 90). 메시지 없는 채널도 비활성으로 본다.
- 아카이브 1차 대상은 텍스트 채널(type 0). 음성(2)/스테이지(13)는 죽은 텍스트와 '이름쌍'
  (예: chess-engine-algo-채팅 ↔ CHESS-ENGINE-ALGO-음성)일 때 동반 아카이브 — 텍스트 짝이
  없는 일반 음성 라운지는 통화-전용 활성일 수 있어 건드리지 않는다. 포럼(15)/공지(5) 제외.
- 이미 '📦 아카이브' 안에 있는 채널은 퍼지(삭제) 대상으로 분류(다시 아카이브하지 않음).
- 역할은 멤버 0명 + 봇/연동 아님 + @everyone/관리역할/보호역할 아님 + 살아있는(아카이브/퍼지
  대상이 아닌) 채널이 안 쓰는 것만 삭제 후보. 역할 삭제는 퍼지 단계에서만 일어난다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DISCORD_EPOCH_MS = 1420070400000
DEFAULT_INACTIVE_DAYS = 90

# 아카이브 카테고리 이름. 50개 초과 시 "📦 아카이브 2", "📦 아카이브 3" … 로 분할(카테고리당 한도).
ARCHIVE_CATEGORY_BASE = "📦 아카이브"
MAX_CHANNELS_PER_CATEGORY = 50

# 아카이브 후보로 고려하는 채널 타입: 텍스트만.
ARCHIVABLE_TYPES = frozenset({0})

# 동반 아카이브 타입: 음성(2)/스테이지(13). 죽은 텍스트와 '이름쌍'일 때만 함께 아카이브한다.
CO_ARCHIVE_TYPES = frozenset({2, 13})

# 아카이브에서 항상 제외할 채널 이름 조각(운영/안내/입구 등). 대소문자 무시 부분일치 — 드라이런에서
# 운영자가 최종 검토하므로 보수적으로 넓게 잡는다.
PROTECTED_NAME_PARTS = (
    "공지", "announce", "입구", "welcome", "환영", "규칙", "rule",
    "역할", "role", "관리", "운영", "moderator", "admin", "봇", "bot", "보관", "archive", "아카이브",
)


@dataclass(frozen=True)
class ChannelAction:
    id: int
    name: str
    age_days: float | None  # None = 메시지 흔적 없음


@dataclass(frozen=True)
class RoleAction:
    id: int
    name: str
    reason: str


@dataclass
class CleanupPlan:
    inactive_days: int
    archive_channels: list[ChannelAction] = field(default_factory=list)  # → 아카이브로 이동(되돌림 가능)
    purge_channels: list[ChannelAction] = field(default_factory=list)  # 이미 아카이브에 있음 → 삭제(영구)
    delete_roles: list[RoleAction] = field(default_factory=list)  # 고아 역할 → 삭제(영구)
    orphan_categories: list[ChannelAction] = field(default_factory=list)  # 빈(고아) 카테고리 → 삭제(영구)
    skipped_protected: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (
            self.archive_channels or self.purge_channels or self.delete_roles or self.orphan_categories
        )

    def _chan_line(self, c: ChannelAction) -> str:
        age = "메시지 없음" if c.age_days is None else f"{c.age_days:.0f}일 전"
        return f"  • #{c.name} (마지막 활동 {age})"

    def summary(self) -> str:
        """리포트용 전체 요약(아카이브 예정 + 퍼지 예정 + 역할)."""
        lines = [f"🧹 정리 현황 (비활성 기준 {self.inactive_days}일)"]
        lines.append(f"\n📦 아카이브로 옮길 비활성 채널: {len(self.archive_channels)}개")
        for c in self.archive_channels[:20]:
            lines.append(self._chan_line(c))
        if len(self.archive_channels) > 20:
            lines.append(f"  …외 {len(self.archive_channels) - 20}개")
        lines.append(f"\n🔥 이미 아카이브에 있어 퍼지(삭제)될 채널: {len(self.purge_channels)}개")
        for c in self.purge_channels[:20]:
            lines.append(f"  • #{c.name}")
        if len(self.purge_channels) > 20:
            lines.append(f"  …외 {len(self.purge_channels) - 20}개")
        lines.append(f"\n🗑️ 퍼지 때 삭제될 고아 역할: {len(self.delete_roles)}개")
        for r in self.delete_roles[:20]:
            lines.append(f"  • @{r.name} ({r.reason})")
        if len(self.delete_roles) > 20:
            lines.append(f"  …외 {len(self.delete_roles) - 20}개")
        lines.append(f"\n🗂️ 퍼지 때 삭제될 빈 카테고리: {len(self.orphan_categories)}개")
        for c in self.orphan_categories[:20]:
            lines.append(f"  • {c.name}")
        if len(self.orphan_categories) > 20:
            lines.append(f"  …외 {len(self.orphan_categories) - 20}개")
        lines.append("\n명령: /cleanup-archive (이동·되돌림 가능) → /cleanup-purge (영구 삭제)")
        return "\n".join(lines)

    def archive_summary(self) -> str:
        if not self.archive_channels:
            return "아카이브로 옮길 비활성 채널이 없어 ✅"
        lines = [f"📦 아카이브로 옮길 채널 {len(self.archive_channels)}개 (멤버에게 숨김, 되돌림 가능):"]
        for c in self.archive_channels[:30]:
            lines.append(self._chan_line(c))
        if len(self.archive_channels) > 30:
            lines.append(f"  …외 {len(self.archive_channels) - 30}개")
        return "\n".join(lines)

    def purge_summary(self) -> str:
        if not self.purge_channels and not self.delete_roles and not self.orphan_categories:
            return "아카이브가 비어 있고 삭제할 고아 역할/카테고리도 없어 ✅"
        lines = ["🔥 영구 삭제 (되돌릴 수 없음):"]
        if self.purge_channels:
            lines.append(f"\n아카이브 채널 {len(self.purge_channels)}개:")
            for c in self.purge_channels[:30]:
                lines.append(f"  • #{c.name}")
            if len(self.purge_channels) > 30:
                lines.append(f"  …외 {len(self.purge_channels) - 30}개")
        if self.delete_roles:
            lines.append(f"\n고아 역할 {len(self.delete_roles)}개:")
            for r in self.delete_roles[:30]:
                lines.append(f"  • @{r.name} ({r.reason})")
            if len(self.delete_roles) > 30:
                lines.append(f"  …외 {len(self.delete_roles) - 30}개")
        if self.orphan_categories:
            lines.append(f"\n빈 카테고리 {len(self.orphan_categories)}개:")
            for c in self.orphan_categories[:30]:
                lines.append(f"  • {c.name}")
            if len(self.orphan_categories) > 30:
                lines.append(f"  …외 {len(self.orphan_categories) - 30}개")
        return "\n".join(lines)


def age_days(last_message_id, now_ms: float) -> float | None:
    """Discord 스노플레이크(last_message_id)에서 마지막 활동 경과일. 없으면 None."""
    if not last_message_id:
        return None
    ts = (int(last_message_id) >> 22) + DISCORD_EPOCH_MS
    return (now_ms - ts) / 86400000.0


def _is_protected(name: str, protected_parts) -> bool:
    low = (name or "").lower()
    return any(p.lower() in low for p in protected_parts)


def _base_name(name: str) -> str:
    """채널 이름에서 종류 접미사(음성/voice/채팅/chat/채널)를 떼고 소문자화 — 텍스트↔음성 이름쌍 매칭용."""
    s = (name or "").lower().strip()
    for suf in ("-음성", " 음성", "음성", "-voice", "voice", "-채팅", " 채팅", "채팅", "-chat", "chat", "-채널", "채널"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s.strip(" -_·")


def find_archive_category_ids(channels: list[dict]) -> set[int]:
    """'📦 아카이브'(및 분할본) 카테고리의 id 집합."""
    return {
        int(c["id"])
        for c in channels
        if int(c.get("type", -1)) == 4 and (c.get("name") or "").startswith(ARCHIVE_CATEGORY_BASE)
    }


def plan_cleanup(
    channels: list[dict],
    roles: list[dict],
    *,
    now_ms: float,
    inactive_days: int = DEFAULT_INACTIVE_DAYS,
    admin_role_id: int = 0,
    welcome_channel_id: int = 0,
    protected_parts=PROTECTED_NAME_PARTS,
    protected_role_ids=(),
) -> CleanupPlan:
    """채널/역할 dict 위에서 정리 계획을 만든다(순수 함수, 디스코드 호출 없음).

    채널 dict 기대 키: id, name, type(int), parent_id(str|None), last_message_id(str|None),
                       overwrite_role_ids(list[str]).
    역할 dict 기대 키: id, name, member_count(int), managed(bool), is_default(bool).
    """
    plan = CleanupPlan(inactive_days=inactive_days)
    archive_cat_ids = find_archive_category_ids(channels)

    for c in channels:
        cid = int(c["id"])
        parent = c.get("parent_id")
        # 이미 아카이브 안 → 퍼지(삭제) 대상; 다시 아카이브하지 않음.
        if parent and int(parent) in archive_cat_ids:
            plan.purge_channels.append(
                ChannelAction(cid, c.get("name", ""), age_days(c.get("last_message_id"), now_ms))
            )
            continue
        if int(c.get("type", -1)) not in ARCHIVABLE_TYPES:
            continue
        name = c.get("name", "")
        if welcome_channel_id and cid == int(welcome_channel_id):
            continue
        if _is_protected(name, protected_parts):
            plan.skipped_protected.append(name)
            continue
        a = age_days(c.get("last_message_id"), now_ms)
        if a is None or a >= inactive_days:
            plan.archive_channels.append(ChannelAction(cid, name, a))

    # 음성/스테이지도 아카이브: (1) 음성-텍스트챗 마지막 활동이 오래됨(≥기준), 또는 (2) 텍스트
    # 흔적은 없지만 죽은 텍스트와 이름쌍(chess-engine-algo-채팅 ↔ CHESS-ENGINE-ALGO-음성).
    # 텍스트 짝도 흔적도 없는 음성(일반 라운지)은 통화-전용 활성일 수 있어 건드리지 않는다.
    archived_text_bases = {_base_name(ca.name) for ca in plan.archive_channels}
    for c in channels:
        if int(c.get("type", -1)) not in CO_ARCHIVE_TYPES:
            continue
        cid = int(c["id"])
        parent = c.get("parent_id")
        if parent and int(parent) in archive_cat_ids:
            continue
        name = c.get("name", "")
        if _is_protected(name, protected_parts):
            continue
        a = age_days(c.get("last_message_id"), now_ms)
        if a is not None and a < inactive_days:
            continue  # 최근 음성-텍스트챗 활동 → 유지
        if a is not None or (_base_name(name) in archived_text_bases):
            plan.archive_channels.append(ChannelAction(cid, name, a))

    # 살아있는(아카이브/퍼지 대상이 아닌) 채널이 권한 오버라이트에 쓰는 역할은 보존.
    moving_or_purging = {ca.id for ca in plan.archive_channels} | {cp.id for cp in plan.purge_channels}
    live_role_refs: set[int] = set()
    used_anywhere: set[int] = set()
    for c in channels:
        cid = int(c["id"])
        for rid in c.get("overwrite_role_ids") or []:
            used_anywhere.add(int(rid))
            if cid not in moving_or_purging:
                live_role_refs.add(int(rid))

    protected_ids = {int(x) for x in (protected_role_ids or ())}
    for r in roles:
        if r.get("is_default") or r.get("managed"):
            continue
        rid = int(r["id"])
        if admin_role_id and rid == int(admin_role_id):
            continue
        if rid in protected_ids:
            continue  # 외부 보호 역할(예: 레벨 보상) — 멤버 0명이어도 삭제 금지
        if int(r.get("member_count", 0)) != 0:
            continue
        if rid in live_role_refs:
            continue  # 살아있는 채널이 쓰는 역할은 보존
        reason = "멤버 0명·죽은 채널 전용" if rid in used_anywhere else "멤버 0명·미사용"
        plan.delete_roles.append(RoleAction(rid, r.get("name", str(rid)), reason))

    # 고아(빈) 카테고리: 자식 채널이 0인 카테고리. 아카이브 카테고리·보호 이름은 제외.
    child_counts: dict[int, int] = {}
    for c in channels:
        p = c.get("parent_id")
        if p:
            child_counts[int(p)] = child_counts.get(int(p), 0) + 1
    for c in channels:
        if int(c.get("type", -1)) != 4:
            continue
        cid = int(c["id"])
        if cid in archive_cat_ids or _is_protected(c.get("name", ""), protected_parts):
            continue
        if child_counts.get(cid, 0) == 0:
            plan.orphan_categories.append(ChannelAction(cid, c.get("name", ""), None))

    return plan
