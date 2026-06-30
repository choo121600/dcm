"""비활성 채널 보관 + 고아 역할 삭제 '계획' 수립 (discord-free, 순수 데이터).

어댑터가 넘긴 채널/역할 dict 리스트 위에서 *무엇을* 정리할지 결정한다. 디스코드 호출은 전혀
없고, GuildAdminService 가 이 계획을 받아 실제 보관(숨김)/삭제를 실행한다. 순수 함수라 단위
테스트가 쉽다.

설계 결정(사용자 합의):
- 비활성 기준 = 마지막 메시지 경과일 ≥ N일(기본 90). 메시지 없는 채널도 비활성으로 본다.
- 채널은 '보관'(=@everyone view 차단으로 숨김, 되돌릴 수 있음). 카테고리 이동/삭제 안 함.
- 자동 보관 대상은 텍스트 채널(type 0)만. 포럼(15)/공지(5)/음성(2)/카테고리(4)/스테이지(13)는
  활동 판정이 부정확하거나 운영성이라 제외(리포트에는 안 넣음 — 보수적).
- 역할은 멤버 0명 + 봇/연동 아님 + @everyone/관리역할 아님 + (살아있는 채널이 안 쓰는 것)만
  삭제 후보. 살아있는 채널이 권한에 쓰는 역할은 절대 후보 아님.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DISCORD_EPOCH_MS = 1420070400000
DEFAULT_INACTIVE_DAYS = 90

# 보관(아카이브) 후보로 고려하는 채널 타입: 텍스트만.
ARCHIVABLE_TYPES = frozenset({0})

# 보관에서 항상 제외할 채널 이름 조각(운영/안내/입구 등). 대소문자 무시 부분일치 — 드라이런에서
# 운영자가 최종 검토하므로 보수적으로 넓게 잡는다.
PROTECTED_NAME_PARTS = (
    "공지", "announce", "입구", "welcome", "환영", "규칙", "rule",
    "역할", "role", "관리", "운영", "moderator", "admin", "봇", "bot", "보관", "archive",
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
    archive_channels: list[ChannelAction] = field(default_factory=list)
    delete_roles: list[RoleAction] = field(default_factory=list)
    skipped_protected: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.archive_channels and not self.delete_roles

    def summary(self) -> str:
        lines: list[str] = [f"🧹 정리 계획 (비활성 기준 {self.inactive_days}일)"]
        if self.archive_channels:
            lines.append(f"\n📦 보관(숨김)할 채널 {len(self.archive_channels)}개:")
            for c in self.archive_channels[:25]:
                age = "메시지 없음" if c.age_days is None else f"{c.age_days:.0f}일 전"
                lines.append(f"  • #{c.name} (마지막 활동 {age})")
            if len(self.archive_channels) > 25:
                lines.append(f"  …외 {len(self.archive_channels) - 25}개")
        if self.delete_roles:
            lines.append(f"\n🗑️ 삭제할 고아 역할 {len(self.delete_roles)}개:")
            for r in self.delete_roles[:30]:
                lines.append(f"  • @{r.name} ({r.reason})")
            if len(self.delete_roles) > 30:
                lines.append(f"  …외 {len(self.delete_roles) - 30}개")
        if self.empty:
            lines.append("\n정리할 비활성 채널이나 고아 역할이 없어 ✅")
        else:
            lines.append("\n⚠️ 채널 보관은 되돌릴 수 있지만(숨김 해제), 역할 삭제는 되돌릴 수 없어.")
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

    채널 dict 기대 키: id, name, type(int), last_message_id(str|None), overwrite_role_ids(list[str]).
    역할 dict 기대 키: id, name, member_count(int), managed(bool), is_default(bool).
    """
    plan = CleanupPlan(inactive_days=inactive_days)
    archive_ids: set[int] = set()

    for c in channels:
        if int(c.get("type", -1)) not in ARCHIVABLE_TYPES:
            continue
        cid = int(c["id"])
        name = c.get("name", "")
        if welcome_channel_id and cid == int(welcome_channel_id):
            continue
        if _is_protected(name, protected_parts):
            plan.skipped_protected.append(name)
            continue
        a = age_days(c.get("last_message_id"), now_ms)
        if a is None or a >= inactive_days:
            plan.archive_channels.append(ChannelAction(cid, name, a))
            archive_ids.add(cid)

    # 살아있는(보관 대상이 아닌) 채널이 권한 오버라이트에 쓰는 역할 id 집합.
    live_role_refs: set[int] = set()
    used_anywhere: set[int] = set()
    for c in channels:
        cid = int(c["id"])
        for rid in c.get("overwrite_role_ids") or []:
            used_anywhere.add(int(rid))
            if cid not in archive_ids:
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

    return plan
