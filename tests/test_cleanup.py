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


def _chan(cid, name, *, type=0, days_ago=None, overwrite_role_ids=(), parent_id=None):
    return {
        "id": str(cid),
        "name": name,
        "type": type,
        "parent_id": str(parent_id) if parent_id is not None else None,
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
    assert [(r.name, r.reason) for r in plan.delete_roles] == [("chess", "죽은 채널 전용·멤버 0명")]
    assert [c.name for c in plan.archive_channels] == ["dead"]


def test_dead_channel_role_with_members_is_deleted():
    """죽은 채널 전용 역할은 멤버가 남아 있어도 정리 대상(채널이 사라지면 무용)."""
    chans = [_chan(1, "django-채팅", type=0, days_ago=300, overwrite_role_ids=[9])]
    plan = plan_cleanup(chans, [_role(9, "DJANGO", member_count=6)], now_ms=NOW_MS, inactive_days=90)
    assert [r.name for r in plan.delete_roles] == ["DJANGO"]
    assert "멤버 6명" in plan.delete_roles[0].reason


def test_noncohort_member_role_unused_is_removed():
    """계절(기수)명이 없는 채널-미사용 멤버 역할은 제거 후보(사용자 정책)."""
    plan = plan_cleanup([], [_role(9, "PYTHON", member_count=24)], now_ms=NOW_MS, inactive_days=90)
    assert [r.name for r in plan.delete_roles] == ["PYTHON"]
    assert "비기수" in plan.delete_roles[0].reason


def test_cohort_season_role_is_kept():
    """이름에 계절(Summer/Winter 등)이 있는 기수 역할은 멤버 보유·채널 미사용이어도 보존."""
    roles = [_role(1, "2024 Summer", member_count=40), _role(2, "25-WINTER", member_count=30)]
    plan = plan_cleanup([], roles, now_ms=NOW_MS, inactive_days=90)
    assert plan.delete_roles == []


def test_staff_named_role_is_kept():
    """이름 보호어(운영/관리/모더)가 든 역할은 멤버 보유·채널 미사용이어도 보존."""
    roles = [_role(1, "운영진", member_count=8), _role(2, "관리자", member_count=3)]
    plan = plan_cleanup([], roles, now_ms=NOW_MS, inactive_days=90)
    assert plan.delete_roles == []


# ── summary / empty ─────────────────────────────────────────────────────
def test_empty_plan():
    plan = plan_cleanup([], [], now_ms=NOW_MS)
    assert plan.empty
    assert "0개" in plan.summary()  # 빈 계획: 모든 섹션 0개


def test_summary_lists_counts():
    chans = [_chan(1, "dead", days_ago=300)]
    roles = [_role(9, "orphan", member_count=0)]
    plan = plan_cleanup(chans, roles, now_ms=NOW_MS, inactive_days=90)
    s = plan.summary()
    assert "비활성 채널: 1개" in s and "고아 역할: 1개" in s and "#dead" in s and "@orphan" in s


def test_protected_role_ids_are_kept():
    """protected_role_ids(예: 레벨 보상 역할)는 멤버 0명이어도 삭제 후보에서 제외된다."""
    plan = plan_cleanup([], [_role(9, "Lv10 보상", member_count=0)], now_ms=NOW_MS, protected_role_ids={9})
    assert plan.delete_roles == []


def test_channel_in_archive_is_purge_target_not_archive():
    """이미 '📦 아카이브' 안에 있는 채널은 퍼지(삭제) 대상이고 다시 아카이브하지 않는다."""
    chans = [
        _chan(50, "📦 아카이브", type=4),
        _chan(1, "old-dead", type=0, days_ago=300, parent_id=50),  # 아카이브 안 → 퍼지
        _chan(2, "fresh-dead", type=0, days_ago=300),  # 아카이브 밖 → 아카이브
    ]
    plan = plan_cleanup(chans, [], now_ms=NOW_MS, inactive_days=90)
    assert [c.name for c in plan.purge_channels] == ["old-dead"]
    assert [c.name for c in plan.archive_channels] == ["fresh-dead"]


# ── 서비스 실행(드라이런→확인) ───────────────────────────────────────────
class _FakeAdmin:
    def __init__(self, channels, roles):
        self._channels = channels
        self._roles = roles
        self.hidden: list = []
        self.deleted_roles: list = []
        self.deleted_channels: list = []
        self.created_categories: list = []
        self.moved: list = []
        self._next_cat = 9000

    async def list_channels(self, guild_id):
        return self._channels

    async def list_roles(self, guild_id):
        return self._roles

    async def create_category(self, guild_id, name, *, reason):
        self._next_cat += 1
        self.created_categories.append((self._next_cat, name))
        return str(self._next_cat)

    async def edit_channel(self, guild_id, channel_id, *, name=None, category_id=None, reason):
        self.moved.append((channel_id, category_id))

    async def set_channel_role_overwrite(self, guild_id, channel_id, role_id, *, view, reason):
        self.hidden.append((channel_id, role_id, view))

    async def delete_channel(self, guild_id, channel_id, *, reason):
        self.deleted_channels.append(channel_id)

    async def delete_role(self, guild_id, role_id, *, reason):
        self.deleted_roles.append(role_id)


def test_service_archive_dryrun_then_confirm_moves_and_hides():
    """cleanup_archive: 토큰 없으면 미리보기(실행X); 토큰 주면 아카이브 카테고리 생성 후 이동+숨김."""
    admin = _FakeAdmin([_chan(1, "dead", days_ago=None)], [])
    svc = GuildAdminService(admin, PendingConfirmations())

    async def scenario():
        res = await svc.cleanup_archive(guild_id=100, actor_name="a", actor_id=1)
        assert res.needs_confirmation and res.confirmation_token
        assert admin.moved == [] and admin.created_categories == []  # 드라이런: 실행 안 함
        res2 = await svc.cleanup_archive(
            guild_id=100, actor_name="a", actor_id=1, confirm_token=res.confirmation_token
        )
        assert res2.ok
        assert len(admin.created_categories) == 1  # 아카이브 카테고리 1개 생성
        new_cat = admin.created_categories[0][0]
        assert admin.moved == [(1, new_cat)]  # dead 채널을 아카이브로 이동
        # 카테고리·채널 모두 @everyone(=100) 숨김; 삭제는 안 함
        assert (1, 100, False) in admin.hidden and (new_cat, 100, False) in admin.hidden
        assert admin.deleted_channels == []

    asyncio.run(scenario())


def test_service_purge_dryrun_then_confirm_deletes():
    """cleanup_purge: 아카이브 안 채널 + 고아 역할 + 빈/고아 카테고리 삭제."""
    chans = [
        _chan(50, "📦 아카이브", type=4),
        _chan(1, "archived", type=0, days_ago=300, parent_id=50),
        _chan(52, "빈카테고리", type=4),  # 고아(빈) 카테고리
    ]
    admin = _FakeAdmin(chans, [_role(9, "orphan", member_count=0)])
    svc = GuildAdminService(admin, PendingConfirmations())

    async def scenario():
        res = await svc.cleanup_purge(guild_id=100, actor_name="a", actor_id=1)
        assert res.needs_confirmation and res.confirmation_token
        assert admin.deleted_channels == [] and admin.deleted_roles == []  # 드라이런
        res2 = await svc.cleanup_purge(
            guild_id=100, actor_name="a", actor_id=1, confirm_token=res.confirmation_token
        )
        assert res2.ok
        assert 1 in admin.deleted_channels  # 아카이브 안 채널 삭제
        assert 50 in admin.deleted_channels  # 비워진 아카이브 카테고리 삭제
        assert 52 in admin.deleted_channels  # 고아(빈) 카테고리 삭제
        assert admin.deleted_roles == [9]

    asyncio.run(scenario())


def test_service_cleanup_report_is_readonly():
    """cleanup_report 는 변경 없이 요약만 반환한다."""
    admin = _FakeAdmin([_chan(1, "dead", days_ago=None)], [_role(9, "orphan", member_count=0)])
    svc = GuildAdminService(admin, PendingConfirmations())
    res = asyncio.run(svc.cleanup_report(guild_id=100))
    assert res.ok and not res.needs_confirmation
    assert admin.moved == [] and admin.deleted_channels == [] and admin.deleted_roles == []
    assert "#dead" in res.detail and "@orphan" in res.detail


# ── 음성 동반 아카이브(이름쌍) ───────────────────────────────────────────
def test_voice_co_archived_with_dead_text_sibling():
    """죽은 텍스트와 이름쌍인 음성(텍스트 없어도)은 함께 아카이브된다."""
    chans = [
        _chan(1, "chess-engine-algo-채팅", type=0, days_ago=300),
        _chan(2, "CHESS-ENGINE-ALGO-음성", type=2, days_ago=None),
    ]
    plan = plan_cleanup(chans, [], now_ms=NOW_MS, inactive_days=90)
    assert {c.name for c in plan.archive_channels} == {"chess-engine-algo-채팅", "CHESS-ENGINE-ALGO-음성"}


def test_voice_lounge_without_text_sibling_is_kept():
    """텍스트 짝이 없는 일반 음성(라운지)은 통화-전용 활성일 수 있어 건드리지 않는다."""
    chans = [
        _chan(1, "django-채팅", type=0, days_ago=300),
        _chan(2, "라운지", type=2, days_ago=None),
    ]
    plan = plan_cleanup(chans, [], now_ms=NOW_MS, inactive_days=90)
    assert [c.name for c in plan.archive_channels] == ["django-채팅"]


def test_voice_with_recent_activity_is_kept_even_with_sibling():
    """음성-텍스트챗에 최근 활동이 있으면 죽은 텍스트 짝이 있어도 유지."""
    chans = [
        _chan(1, "django-채팅", type=0, days_ago=300),
        _chan(2, "DJANGO-음성", type=2, days_ago=5),
    ]
    plan = plan_cleanup(chans, [], now_ms=NOW_MS, inactive_days=90)
    assert [c.name for c in plan.archive_channels] == ["django-채팅"]


def test_co_archiving_voice_makes_shared_role_orphan():
    """죽은 텍스트+음성에서만 쓰이는 역할은 둘 다 아카이브되면 고아(삭제 후보)가 된다."""
    chans = [
        _chan(1, "chess-채팅", type=0, days_ago=300, overwrite_role_ids=[9]),
        _chan(2, "CHESS-음성", type=2, days_ago=None, overwrite_role_ids=[9]),
    ]
    plan = plan_cleanup(chans, [_role(9, "CHESS", member_count=0)], now_ms=NOW_MS, inactive_days=90)
    assert {c.name for c in plan.archive_channels} == {"chess-채팅", "CHESS-음성"}
    assert [r.name for r in plan.delete_roles] == ["CHESS"]


# ── 단독 오래된 음성 + 고아(빈) 카테고리 ─────────────────────────────────
def test_standalone_old_voice_is_archived_without_sibling():
    """이름쌍이 없어도 음성-텍스트챗 마지막 활동이 오래되면(≥기준) 아카이브된다."""
    chans = [_chan(1, "geeknews-radio", type=2, days_ago=300)]  # 텍스트 짝 없음, 오래된 음성
    plan = plan_cleanup(chans, [], now_ms=NOW_MS, inactive_days=90)
    assert [c.name for c in plan.archive_channels] == ["geeknews-radio"]


def test_no_text_voice_without_sibling_is_kept():
    """텍스트 흔적도 짝도 없는 음성(일반 라운지)은 통화-전용 활성일 수 있어 유지."""
    chans = [_chan(1, "라운지", type=2, days_ago=None)]
    plan = plan_cleanup(chans, [], now_ms=NOW_MS, inactive_days=90)
    assert plan.archive_channels == []


def test_orphan_empty_category_detected():
    chans = [
        _chan(50, "빈카테고리", type=4),  # 자식 없음 → 고아
        _chan(51, "활성카테고리", type=4),
        _chan(1, "general", type=0, days_ago=1, parent_id=51),  # 자식 있음 → 유지
    ]
    plan = plan_cleanup(chans, [], now_ms=NOW_MS, inactive_days=90)
    assert [c.name for c in plan.orphan_categories] == ["빈카테고리"]


def test_archive_category_not_treated_as_orphan():
    chans = [_chan(50, "📦 아카이브", type=4)]  # 빈 아카이브 카테고리지만 고아로 분류 안 함
    plan = plan_cleanup(chans, [], now_ms=NOW_MS, inactive_days=90)
    assert plan.orphan_categories == []


def test_notext_voice_in_dead_category_is_archived():
    """텍스트 흔적 없는 음성이라도 '최근 활동 채널이 없는' 카테고리(예: 토픽 라운지)면 아카이브."""
    chans = [
        _chan(50, "PYTHON", type=4),
        _chan(1, "라운지", type=2, days_ago=None, parent_id=50),
    ]
    plan = plan_cleanup(chans, [], now_ms=NOW_MS, inactive_days=90)
    assert [c.name for c in plan.archive_channels] == ["라운지"]


def test_notext_voice_in_active_category_is_kept():
    """카테고리에 최근 활동 채널이 있으면 같은 카테고리의 텍스트-없는 음성은 보존."""
    chans = [
        _chan(50, "📞음성 채널", type=4),
        _chan(1, "모각코", type=2, days_ago=2, parent_id=50),  # 최근 활동
        _chan(2, "스터디룸2", type=2, days_ago=None, parent_id=50),  # 텍스트 없음
    ]
    plan = plan_cleanup(chans, [], now_ms=NOW_MS, inactive_days=90)
    names = {c.name for c in plan.archive_channels}
    assert "스터디룸2" not in names and "모각코" not in names


def test_subproject_cohort_role_is_removed_umbrella_kept():
    """계절 뒤 프로젝트명이 붙은 세부 기수(25-SUMMER-DJANGO)는 제거; 엄브렐러(25-SUMMER)는 보존."""
    roles = [_role(1, "25-SUMMER", member_count=65), _role(2, "25-SUMMER-DJANGO", member_count=6)]
    plan = plan_cleanup([], roles, now_ms=NOW_MS, inactive_days=90)
    names = [r.name for r in plan.delete_roles]
    assert "25-SUMMER-DJANGO" in names and "25-SUMMER" not in names
