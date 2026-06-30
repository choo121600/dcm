"""service/cleanup.py 계획기 테스트 — 비활성 채널/고아 역할 판정 로직(순수 함수)."""
from __future__ import annotations

import asyncio

from dcm.service.cleanup import (
    DISCORD_EPOCH_MS,
    age_days,
    plan_cleanup,
)
from dcm.service.guild_admin import GuildAdminService, PendingConfirmations

NOW_MS = 1_900_000_000_000  # 고정 기준시각


def _snowflake(days_ago: float) -> str:
    """days_ago 일 전에 보낸 메시지의 스노플레이크 id 문자열."""
    ts_ms = NOW_MS - days_ago * 86400000
    return str((int(ts_ms) - DISCORD_EPOCH_MS) << 22)


def _chan(cid, name, *, type=0, days_ago=None, overwrite_role_ids=()):
    return {
        "id": str(cid),
        "name": name,
        "type": type,
        "last_message_id": _snowflake(days_ago) if days_ago is not None else None,
        "overwrite_role_ids": [str(r) for r in overwrite_role_ids],
    }


def _role(rid, name, *, member_count=0, managed=False, is_default=False):
    return {"id": str(rid), "name": name, "member_count": member_count, "managed": managed, "is_default": is_default}


# ── age 계산 ───────────────────────────────────────────────────────────
def test_age_days_roundtrip():
    assert age_days(None, NOW_MS) is None
    assert abs(age_days(_snowflake(100), NOW_MS) - 100) < 0.01


# ── 채널 보관 판정 ──────────────────────────────────────────────────────
def test_old_text_channel_is_archived():
    plan = plan_cleanup([_chan(1, "dead", days_ago=200)], [], now_ms=NOW_MS, inactive_days=90)
    assert [c.name for c in plan.archive_channels] == ["dead"]


def test_recent_text_channel_is_kept():
    plan = plan_cleanup([_chan(1, "alive", days_ago=10)], [], now_ms=NOW_MS, inactive_days=90)
    assert plan.archive_channels == []


def test_empty_channel_no_messages_is_archived():
    plan = plan_cleanup([_chan(1, "empty", days_ago=None)], [], now_ms=NOW_MS, inactive_days=90)
    assert [c.name for c in plan.archive_channels] == ["empty"]
    assert plan.archive_channels[0].age_days is None


def test_non_text_types_excluded():
    chans = [
        _chan(1, "voice", type=2, days_ago=None),
        _chan(2, "category", type=4, days_ago=None),
        _chan(3, "forum", type=15, days_ago=None),
        _chan(4, "announce", type=5, days_ago=999),
    ]
    plan = plan_cleanup(chans, [], now_ms=NOW_MS, inactive_days=90)
    assert plan.archive_channels == []  # 텍스트(0)만 후보


def test_protected_names_skipped():
    chans = [
        _chan(1, "공지", days_ago=999),
        _chan(2, "입구-welcome", days_ago=999),
        _chan(3, "운영진-회의", days_ago=999),
        _chan(4, "그냥잡담", days_ago=999),
    ]
    plan = plan_cleanup(chans, [], now_ms=NOW_MS, inactive_days=90)
    assert [c.name for c in plan.archive_channels] == ["그냥잡담"]
    assert set(plan.skipped_protected) == {"공지", "입구-welcome", "운영진-회의"}


def test_welcome_channel_id_skipped():
    plan = plan_cleanup([_chan(77, "잡담", days_ago=999)], [], now_ms=NOW_MS, inactive_days=90, welcome_channel_id=77)
    assert plan.archive_channels == []


# ── 역할 삭제 판정 ──────────────────────────────────────────────────────
def test_orphan_role_unused_is_deleted():
    plan = plan_cleanup([], [_role(5, "후원사", member_count=0)], now_ms=NOW_MS)
    assert [(r.name, r.reason) for r in plan.delete_roles] == [("후원사", "멤버 0명·미사용")]


def test_role_with_members_is_kept():
    plan = plan_cleanup([], [_role(5, "운영진", member_count=8)], now_ms=NOW_MS)
    assert plan.delete_roles == []


def test_managed_and_default_roles_kept():
    roles = [_role(5, "Bot", member_count=0, managed=True), _role(6, "@everyone", member_count=0, is_default=True)]
    plan = plan_cleanup([], roles, now_ms=NOW_MS)
    assert plan.delete_roles == []


def test_admin_role_never_deleted():
    plan = plan_cleanup([], [_role(42, "관리자", member_count=0)], now_ms=NOW_MS, admin_role_id=42)
    assert plan.delete_roles == []


def test_role_used_by_live_channel_is_kept():
    chans = [_chan(1, "alive", days_ago=1, overwrite_role_ids=[9])]
    plan = plan_cleanup(chans, [_role(9, "gate", member_count=0)], now_ms=NOW_MS, inactive_days=90)
    assert plan.delete_roles == []  # 살아있는 채널이 쓰는 역할은 보존


def test_role_used_only_by_archived_channel_is_deleted():
    chans = [_chan(1, "dead", days_ago=300, overwrite_role_ids=[9])]
    plan = plan_cleanup(chans, [_role(9, "chess", member_count=0)], now_ms=NOW_MS, inactive_days=90)
    assert [(r.name, r.reason) for r in plan.delete_roles] == [("chess", "멤버 0명·죽은 채널 전용")]
    assert [c.name for c in plan.archive_channels] == ["dead"]


# ── summary / empty ─────────────────────────────────────────────────────
def test_empty_plan():
    plan = plan_cleanup([], [], now_ms=NOW_MS)
    assert plan.empty
    assert "없어" in plan.summary()


def test_summary_lists_counts():
    chans = [_chan(1, "dead", days_ago=300)]
    roles = [_role(9, "orphan", member_count=0)]
    plan = plan_cleanup(chans, roles, now_ms=NOW_MS, inactive_days=90)
    s = plan.summary()
    assert "채널 1개" in s and "역할 1개" in s and "#dead" in s and "@orphan" in s
    assert "되돌릴 수 없어" in s  # 역할 삭제 경고


def test_protected_role_ids_are_kept():
    """protected_role_ids(예: 레벨 보상 역할)는 멤버 0명이어도 삭제 후보에서 제외된다."""
    plan = plan_cleanup([], [_role(9, "Lv10 보상", member_count=0)], now_ms=NOW_MS, protected_role_ids={9})
    assert plan.delete_roles == []


# ── 서비스 실행(드라이런→확인) ───────────────────────────────────────────
class _FakeAdmin:
    def __init__(self, channels, roles):
        self._channels = channels
        self._roles = roles
        self.hidden: list = []
        self.deleted_roles: list = []

    async def list_channels(self, guild_id):
        return self._channels

    async def list_roles(self, guild_id):
        return self._roles

    async def set_channel_role_overwrite(self, guild_id, channel_id, role_id, *, view, reason):
        self.hidden.append((channel_id, role_id, view))

    async def delete_role(self, guild_id, role_id, *, reason):
        self.deleted_roles.append(role_id)


def test_service_cleanup_dryrun_then_confirm_executes():
    """cleanup_inactive: 토큰 없으면 미리보기(실행X), 토큰 주면 보관(숨김)+역할 삭제 실행."""
    chans = [_chan(1, "dead", days_ago=None)]
    roles = [_role(9, "orphan", member_count=0)]
    admin = _FakeAdmin(chans, roles)
    svc = GuildAdminService(admin, PendingConfirmations())

    async def scenario():
        res = await svc.cleanup_inactive(guild_id=100, actor_name="a", actor_id=1)
        assert res.needs_confirmation and res.confirmation_token
        assert admin.hidden == [] and admin.deleted_roles == []  # 드라이런: 실행 안 함
        res2 = await svc.cleanup_inactive(
            guild_id=100, actor_name="a", actor_id=1, confirm_token=res.confirmation_token
        )
        assert res2.ok
        # dead 채널(id 1)을 @everyone(role_id==guild_id 100) view=False 로 숨김; alive(2)는 그대로
        assert admin.hidden == [(1, 100, False)]
        assert admin.deleted_roles == [9]

    asyncio.run(scenario())


def test_service_cleanup_report_is_readonly():
    """cleanup_report 는 변경 없이 요약만 반환한다."""
    admin = _FakeAdmin([_chan(1, "dead", days_ago=None)], [_role(9, "orphan", member_count=0)])
    svc = GuildAdminService(admin, PendingConfirmations())
    res = asyncio.run(svc.cleanup_report(guild_id=100))
    assert res.ok and not res.needs_confirmation
    assert admin.hidden == [] and admin.deleted_roles == []
    assert "#dead" in res.detail and "@orphan" in res.detail
