"""신뢰-하락 Phase 2 레드팀 — 역할 회수 안전성·인젝션 게이팅·위험 워드리스트 깨뜨리기 시도.

핵심 불변식: 회수는 (level_role_rewards ∩ _role_grant_ok ∩ lvl<reward-히스테리시스) 만 —
온보딩 default_role·managed·위계 초과·위험권한 역할은 절대 자동 회수 안 함. 인젝션/위험 신호는
decay+신호별 토글 활성 시에만, shadow/cap 적용.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dcm.leveling.scoring import PENALTY_INJECTION
from dcm.leveling.service import PENALTY_WINDOW_CAP, LevelingService
from dcm.leveling.store import LevelingStore
from dcm.service.guild_settings import GuildSettings

_SAFE_NAMES = (
    "administrator", "manage_guild", "manage_roles", "manage_channels",
    "kick_members", "ban_members", "manage_nicknames", "mute_members", "moderate_members",
)


class _Settings:
    def __init__(self, **kw):
        self._gs = GuildSettings(guild_id="1", **kw)

    def get(self, gid):
        return self._gs


def _svc(tmp, **kw):
    store = LevelingStore(os.path.join(tmp, "leveling.db"))
    return LevelingService(store, _Settings(**kw) if kw else None), store


def _perms(**on):
    p = types.SimpleNamespace()
    for name in _SAFE_NAMES:
        setattr(p, name, False)
    for k, v in on.items():
        setattr(p, k, v)
    return p


def _role(role_id=100, position=1, managed=False, **dangerous):
    return types.SimpleNamespace(
        id=role_id, position=position, managed=managed,
        name=f"role{role_id}", permissions=_perms(**dangerous),
    )


def _member(role_lookup, *, roles=None, bot_top=10, default_role=None):
    return types.SimpleNamespace(
        id="42",
        guild=types.SimpleNamespace(
            id="1",
            me=types.SimpleNamespace(top_role=types.SimpleNamespace(position=bot_top)),
            default_role=default_role,
            get_role=lambda rid: role_lookup.get(int(rid)),
        ),
        roles=roles if roles is not None else [],
        add_roles=AsyncMock(),
        remove_roles=AsyncMock(),
    )


# ── 회수 안전성: 보호 역할은 절대 자동 회수 안 함 ──────────────────────


def test_revoke_never_touches_onboarding_default_role():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp)
        try:
            onboarding = _role(role_id=100, position=1)
            store.set_role_reward("1", 5, 100)  # 누군가 실수로 온보딩 역할을 보상에 매핑
            store.add_xp("1", "42", 0, now=1.0)  # 레벨 0
            # 100 번 역할이 default_role(=@everyone 류 온보딩) → _role_grant_ok 'everyone' 거부
            member = _member({100: onboarding}, roles=[onboarding], default_role=onboarding)
            asyncio.run(svc.reconcile_roles(member))
            member.remove_roles.assert_not_awaited()  # 온보딩 역할 보존
        finally:
            store.close()


def test_revoke_never_touches_dangerous_permission_role():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp)
        try:
            store.set_role_reward("1", 5, 100)
            store.add_xp("1", "42", 0, now=1.0)
            danger = _role(role_id=100, position=1, ban_members=True)
            member = _member({100: danger}, roles=[danger])
            asyncio.run(svc.reconcile_roles(member))
            member.remove_roles.assert_not_awaited()  # 위험권한 역할 보존
        finally:
            store.close()


def test_revoke_never_touches_managed_or_above_hierarchy_role():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp)
        try:
            store.set_role_reward("1", 5, 100)
            store.set_role_reward("1", 6, 101)
            store.add_xp("1", "42", 0, now=1.0)
            managed = _role(role_id=100, position=1, managed=True)  # 봇/연동 역할
            above = _role(role_id=101, position=20)  # 봇 최상위(10) 초과
            member = _member({100: managed, 101: above}, roles=[managed, above], bot_top=10)
            asyncio.run(svc.reconcile_roles(member))
            member.remove_roles.assert_not_awaited()  # managed·위계 초과 보존
        finally:
            store.close()


def test_revoke_only_removes_held_reward_role_below_hysteresis():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp)
        try:
            store.set_role_reward("1", 5, 100)
            store.add_xp("1", "42", 0, now=1.0)  # 레벨 0 < 5-1
            safe = _role(role_id=100, position=1)
            member = _member({100: safe}, roles=[safe])
            asyncio.run(svc.reconcile_roles(member))
            member.remove_roles.assert_awaited_once()
            assert member.remove_roles.await_args.args[0] is safe
        finally:
            store.close()


def test_revoke_skipped_when_role_not_held():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp)
        try:
            store.set_role_reward("1", 5, 100)
            store.add_xp("1", "42", 0, now=1.0)
            safe = _role(role_id=100, position=1)
            member = _member({100: safe}, roles=[])  # 미보유
            asyncio.run(svc.reconcile_roles(member))
            member.remove_roles.assert_not_awaited()  # 멱등: 없는 역할 회수 안 함
        finally:
            store.close()


# ── 인젝션/위험 신호 게이팅 ──────────────────────────────────────────


def test_injection_penalty_floored_at_zero_on_low_balance():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp, leveling_decay_enabled=True, leveling_injection_enabled=True)
        try:
            store.add_xp("1", "42", 10, now=1.0)  # 작은 잔고
            svc.apply_signal_penalty("1", "42", PENALTY_INJECTION, signal="injection", now=2.0)
            xp, _ = store.get_record("1", "42")
            assert xp == 0  # 하한0 (음수 불가)
        finally:
            store.close()


def test_injection_penalty_requires_decay_master_toggle():
    with tempfile.TemporaryDirectory() as tmp:
        # injection 토글만 켜고 decay 마스터는 off → 적용 안 됨
        svc, store = _svc(tmp, leveling_injection_enabled=True)
        try:
            store.add_xp("1", "42", 300, now=1.0)
            assert svc.apply_signal_penalty("1", "42", PENALTY_INJECTION, signal="injection", now=2.0) is False
            xp, _ = store.get_record("1", "42")
            assert xp == 300
        finally:
            store.close()


def test_injection_penalty_window_cap_bounds_deduction():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp, leveling_decay_enabled=True, leveling_injection_enabled=True)
        try:
            store.add_xp("1", "42", 1000, now=1.0)
            for _ in range(6):  # 같은 윈도에 반복 → cap
                svc.apply_signal_penalty("1", "42", PENALTY_INJECTION, signal="injection", now=2.0)
            xp, _ = store.get_record("1", "42")
            max_deduct = PENALTY_WINDOW_CAP * abs(PENALTY_INJECTION)
            assert xp >= 1000 - max_deduct  # cap 으로 제한
            assert xp < 1000  # 일부는 차감
        finally:
            store.close()


def test_injection_penalty_guild_isolated():
    with tempfile.TemporaryDirectory() as tmp:
        store = LevelingStore(os.path.join(tmp, "leveling.db"))

        class _MultiSettings:
            def get(self, gid):
                return GuildSettings(
                    guild_id=str(gid), leveling_decay_enabled=True, leveling_injection_enabled=True
                )

        svc = LevelingService(store, _MultiSettings())
        try:
            store.add_xp("g1", "42", 300, now=1.0)
            store.add_xp("g2", "42", 300, now=1.0)
            svc.apply_signal_penalty("g1", "42", PENALTY_INJECTION, signal="injection", now=2.0)
            assert store.get_record("g1", "42")[0] < 300
            assert store.get_record("g2", "42")[0] == 300  # 다른 길드 무영향
        finally:
            store.close()


def test_danger_marker_case_insensitive_only_when_enabled():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp, leveling_decay_enabled=True, leveling_danger_enabled=True)
        try:
            store.add_xp("1", "42", 300, now=1.0)
            svc.record_message("1", "42", "GET FREE NITRO at dlscord.gg", monotonic_time=10.0, now=2.0)
            assert store.get_record("1", "42")[0] < 300  # 대소문자 무관 매칭
        finally:
            store.close()


def test_legit_message_with_no_marker_not_penalized_even_when_danger_on():
    with tempfile.TemporaryDirectory() as tmp:
        svc, store = _svc(tmp, leveling_decay_enabled=True, leveling_danger_enabled=True, leveling_cooldown_seconds=0.0)
        try:
            store.add_xp("1", "42", 300, now=1.0)
            svc.record_message("1", "42", "오늘 스터디 자료 공유합니다 확인 부탁드려요", monotonic_time=10.0, now=2.0)
            assert store.get_record("1", "42")[0] >= 300  # 마커 없으면 무차감(+적립 가능)
        finally:
            store.close()
