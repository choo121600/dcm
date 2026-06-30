"""P3 per-guild 설정 저장소 테스트 (멀티길드)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from dcm.service.guild_settings import GuildSettings, GuildSettingsStore


def _store(path: str, seed: GuildSettings | None = None) -> GuildSettingsStore:
    return GuildSettingsStore(path, seed=seed)


def test_unset_guild_returns_defaults():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(str(Path(tmp) / "s.db"))
        g = s.get(123)
        assert g.guild_id == "123"
        assert g.admin_role_id == 0  # 미설정 → authz 폴백 트리거
        assert g.welcome_channel_id is None and g.default_role_id is None
        s.close()


def test_set_and_get_is_per_guild():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(str(Path(tmp) / "s.db"))
        s.set_admin_role(1, 100)
        s.set_welcome_channel(1, 200)
        s.set_default_role(1, 300)
        s.set_welcome_message(1, "환영")
        g1 = s.get(1)
        assert (g1.admin_role_id, g1.welcome_channel_id, g1.default_role_id, g1.welcome_message) == (
            100,
            200,
            300,
            "환영",
        )
        assert s.get(2).admin_role_id == 0  # 다른 길드는 독립
        s.close()


def test_seed_inserts_once_and_does_not_overwrite_operator_changes():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "s.db")
        _store(path, seed=GuildSettings(guild_id="9", admin_role_id=55)).close()
        s = _store(path)
        s.set_admin_role(9, 77)  # 운영자 변경
        s.close()
        # 재부팅 재시드 — 기존 운영자 값 보존(INSERT OR IGNORE)
        s2 = _store(path, seed=GuildSettings(guild_id="9", admin_role_id=55))
        assert s2.get(9).admin_role_id == 77
        s2.close()


def test_shares_memory_db_file_without_conflict():
    from dcm.memory.store import MemoryStore

    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "memory.db")
        store = MemoryStore(
            path, weights=(0.55, 0.2, 0.25), half_life_base_days=3.0, subject_boost=0.1, seed_guild_id="1"
        )
        gs = GuildSettingsStore(path, seed=GuildSettings(guild_id="1", admin_role_id=42))
        assert gs.get(1).admin_role_id == 42
        assert store.count() == 0  # 별도 테이블, 충돌 없음
        gs.close()
        store.close()
