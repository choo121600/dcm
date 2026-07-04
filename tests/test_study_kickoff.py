"""Study kickoff announcement (/study-kickoff).

Covers the curated schedule data + category-name matching round-trip, the per-study message
builder (confirmed vs. unconfirmed), the chat-channel picker, category→channel resolution, and
the command's preview (no send) / post (pings each study's chat channel) paths.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import discord
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dcm.platform.pycord_adapter import PycordAdapter
from dcm.service.guild_admin import GuildAdminService
from dcm.service.study_lookup import match_study
from dcm.service.study_schedule import STUDY_SCHEDULES, build_study_message

_ADMIN_ROLE = 999


@pytest.fixture(autouse=True)
def loop():
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    lp.close()


# ───────────────────────── curated data + matching ─────────────────────────


def test_schedules_have_expected_shape():
    assert len(STUDY_SCHEDULES) == 10
    confirmed = [s for s in STUDY_SCHEDULES.values() if s.confirmed]
    unconfirmed = [s for s in STUDY_SCHEDULES.values() if not s.confirmed]
    assert len(confirmed) == 9
    # only the LLM study is unconfirmed, and it carries a coordination note
    assert len(unconfirmed) == 1
    assert unconfirmed[0].name.startswith("LLM")
    assert unconfirmed[0].note


def test_every_study_name_matches_back_to_its_file():
    # A Discord category named exactly like the study must resolve to that study via match_study,
    # which is how _resolve_study_channels maps categories → studies.
    for filename, study in STUDY_SCHEDULES.items():
        assert match_study(study.name) == filename, f"{study.name!r} -> {match_study(study.name)!r}"


def test_all_studies_have_mentor():
    for study in STUDY_SCHEDULES.values():
        assert study.mentor


# ───────────────────────── build_study_message ─────────────────────────


def test_build_message_confirmed_contains_all_parts():
    study = STUDY_SCHEDULES["summer_algorithm_basic.mdx"]
    msg = build_study_message(study)
    assert msg.startswith("@here")
    assert study.name in msg
    assert study.schedule in msg
    assert study.mentor in msg
    assert "📸" in msg  # mentor screenshot request
    assert "교류활동" in msg  # kickoff intro (ko default locale)


def test_build_message_unconfirmed_asks_to_coordinate():
    study = STUDY_SCHEDULES["summer_llm.mdx"]
    msg = build_study_message(study)
    assert study.name in msg
    assert "조율" in msg  # "멘토님과 함께 일정을 조율해주세요"
    assert study.note in msg
    assert "📸" in msg


def test_build_message_custom_mention():
    study = STUDY_SCHEDULES["summer_redis.mdx"]
    assert build_study_message(study, mention="@everyone").startswith("@everyone")


# ───────────────────────── channel picking ─────────────────────────


def _text(name, cid=0):
    return types.SimpleNamespace(name=name, id=cid, type=discord.ChannelType.text)


def _voice(name, cid=0):
    return types.SimpleNamespace(name=name, id=cid, type=discord.ChannelType.voice)


def _cat(name, channels):
    return types.SimpleNamespace(name=name, channels=channels)


def test_pick_chat_channel_prefers_chat_named():
    cat = _cat("x", [_text("공지", 1), _text("일반", 2), _voice("회의", 3)])
    assert PycordAdapter._pick_chat_channel(cat).id == 2


def test_pick_chat_channel_falls_back_to_first_text():
    cat = _cat("x", [_voice("회의", 3), _text("정보", 4), _text("자료", 5)])
    assert PycordAdapter._pick_chat_channel(cat).id == 4


def test_pick_chat_channel_none_when_no_text():
    assert PycordAdapter._pick_chat_channel(_cat("x", [_voice("회의", 3)])) is None


# ───────────────────────── command wiring ─────────────────────────


class _AdminCtx:
    def __init__(self, guild):
        self.author = types.SimpleNamespace(
            roles=[types.SimpleNamespace(id=_ADMIN_ROLE)], id=42, display_name="choo"
        )
        self.guild = guild
        self.guild_id = getattr(guild, "id", 123)
        self.responses: list = []

    async def respond(self, text, **kw):
        self.responses.append((text, kw))

    async def defer(self, **kw):
        pass


class _SendChannel:
    def __init__(self, name, cid):
        self.name = name
        self.id = cid
        self.type = discord.ChannelType.text
        self.sent: list = []

    async def send(self, text, **kw):
        self.sent.append((text, kw))


def _guild_with(*categories, gid=123):
    return types.SimpleNamespace(id=gid, categories=list(categories))


def _adapter():
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123, admin_role_id=_ADMIN_ROLE)
    a.register_admin_commands(GuildAdminService(a, a.pending))
    return a


def _cmd(a):
    return next(c for c in a._client.pending_application_commands if c.name == "study-kickoff")


def test_study_kickoff_registered_and_admin_guarded():
    cmd = _cmd(_adapter())
    assert getattr(cmd.callback, "__gjc_admin_guarded__", False) is True


def test_study_kickoff_preview_maps_channels_without_sending(loop):
    algo_chat = _SendChannel("일반", 111)
    redis_chat = _SendChannel("레디스-잡담", 222)
    guild = _guild_with(
        _cat("알고리즘(초급)", [algo_chat]),
        _cat("북스터디 (개발자를 위한 레디스)", [redis_chat]),
        _cat("공지사항", [_SendChannel("공지", 333)]),  # not a study
    )
    a = _adapter()
    ctx = _AdminCtx(guild)
    loop.run_until_complete(_cmd(a).callback(ctx, post=False))
    text, kw = ctx.responses[-1]
    assert kw.get("ephemeral") is True
    assert "<#111>" in text and "<#222>" in text  # matched studies point at their chat channels
    assert "❌" in text  # unmatched studies flagged
    assert algo_chat.sent == [] and redis_chat.sent == []  # preview never sends


def test_study_kickoff_post_pings_each_study_chat_channel(loop):
    algo_chat = _SendChannel("일반", 111)
    redis_chat = _SendChannel("레디스-잡담", 222)
    guild = _guild_with(
        _cat("알고리즘(초급)", [algo_chat]),
        _cat("북스터디 (개발자를 위한 레디스)", [redis_chat]),
    )
    a = _adapter()
    ctx = _AdminCtx(guild)
    loop.run_until_complete(_cmd(a).callback(ctx, post=True))
    # both study chat channels received exactly one @here message naming the study
    assert len(algo_chat.sent) == 1
    assert algo_chat.sent[0][0].startswith("@here")
    assert "알고리즘(초급)" in algo_chat.sent[0][0]
    assert algo_chat.sent[0][1]["allowed_mentions"].everyone is True
    assert len(redis_chat.sent) == 1
    assert "레디스" in redis_chat.sent[0][0]
    text, _ = ctx.responses[-1]
    assert "2" in text  # posted count


def test_study_kickoff_post_no_matches_reports_none(loop):
    guild = _guild_with(_cat("잡담방", [_SendChannel("일반", 1)]))  # no study categories
    a = _adapter()
    ctx = _AdminCtx(guild)
    loop.run_until_complete(_cmd(a).callback(ctx, post=True))
    text, kw = ctx.responses[-1]
    assert kw.get("ephemeral") is True
    assert "매칭" in text  # none-matched message


def test_study_kickoff_requires_guild(loop):
    a = _adapter()
    ctx = _AdminCtx(None)
    ctx.guild = None
    loop.run_until_complete(_cmd(a).callback(ctx, post=True))
    text, _ = ctx.responses[-1]
    assert "서버" in text  # guild_only message
