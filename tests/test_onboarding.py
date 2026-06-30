"""S6 온보딩 오프라인 테스트.

Run: .venv/bin/python -m pytest tests/test_onboarding.py -q

커버리지:
  (a) OnboardingPolicy.decide — 설정 있을 때 welcome_text(이름 치환)+role 포함
  (b) OnboardingPolicy.decide — welcome_channel_id 미설정 시 메시지 스킵(None)
  (c) OnboardingPolicy.decide — default_role_id 미설정 시 역할 스킵(None)
  (d) OnboardingPolicy.decide — 둘 다 미설정 시 모두 None
  (e) {name} 이름 치환 동작
  (f) 알 수 없는 플레이스홀더 포맷 실패 시 원본 텍스트 fallback
  (g) adapter on_member_join — welcome 채널 전송 + 역할 부여
  (h) adapter on_member_join — welcome_channel_id 미설정 시 전송 없음
  (i) adapter on_member_join — default_role_id 미설정 시 역할 부여 없음
  (j) adapter on_member_join — 둘 다 미설정 시 no-op
  (k) adapter on_member_join — 핸들러 예외 시 침묵(봇 죽지 않음)
  (l) adapter on_member_join — onboarding_policy=None이면 즉시 반환
"""
from __future__ import annotations

import asyncio
import types

import pytest

from dcm.service.onboarding import OnboardingDecision, OnboardingPolicy
from dcm.platform.pycord_adapter import PycordAdapter


# ---------------------------------------------------------------------------
# 공통 fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def loop():
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    asyncio.set_event_loop(None)
    lp.close()


def _run(coro, loop):
    return loop.run_until_complete(coro)


# ---------------------------------------------------------------------------
# OnboardingPolicy.decide 단위 테스트
# ---------------------------------------------------------------------------


class TestOnboardingPolicyDecide:
    def test_full_config_returns_welcome_text_and_role(self):
        """welcome_channel_id, welcome_message, default_role_id 모두 설정 시 결정값 반환."""
        pol = OnboardingPolicy(
            welcome_channel_id=111,
            welcome_message="안녕 {name}!",
            default_role_id=999,
        )
        dec = pol.decide("철수")
        assert dec.welcome_channel_id == 111
        assert dec.welcome_text == "안녕 철수!"
        assert dec.default_role_id == 999

    def test_no_welcome_channel_skips_message(self):
        """welcome_channel_id 미설정 → welcome_text/channel_id 모두 None."""
        pol = OnboardingPolicy(
            welcome_channel_id=None,
            welcome_message="안녕 {name}!",
            default_role_id=999,
        )
        dec = pol.decide("철수")
        assert dec.welcome_channel_id is None
        assert dec.welcome_text is None
        assert dec.default_role_id == 999  # 역할은 독립적

    def test_no_default_role_skips_role(self):
        """default_role_id 미설정 → default_role_id None, 메시지는 그대로."""
        pol = OnboardingPolicy(
            welcome_channel_id=111,
            welcome_message="반가워!",
            default_role_id=None,
        )
        dec = pol.decide("영희")
        assert dec.welcome_channel_id == 111
        assert dec.welcome_text == "반가워!"
        assert dec.default_role_id is None

    def test_all_unset_returns_all_none(self):
        """channel/role 모두 미설정 → 모두 None."""
        pol = OnboardingPolicy()
        dec = pol.decide("테스트유저")
        assert dec.welcome_text is None
        assert dec.welcome_channel_id is None
        assert dec.default_role_id is None

    def test_name_substitution(self):
        """{name} 플레이스홀더가 display_name으로 치환된다."""
        pol = OnboardingPolicy(
            welcome_channel_id=1,
            welcome_message="안녕하세요, {name}님! 서버에 오신 걸 환영해요.",
        )
        dec = pol.decide("Hyeonseok")
        assert dec.welcome_text == "안녕하세요, Hyeonseok님! 서버에 오신 걸 환영해요."

    def test_message_without_placeholder(self):
        """플레이스홀더 없는 메시지는 그대로 반환된다."""
        pol = OnboardingPolicy(welcome_channel_id=1, welcome_message="환영합니다!")
        dec = pol.decide("누구")
        assert dec.welcome_text == "환영합니다!"

    def test_unknown_placeholder_falls_back_to_original(self):
        """알 수 없는 플레이스홀더({unknown}) 포맷 실패 시 원본 메시지 반환(폭발 금지)."""
        pol = OnboardingPolicy(
            welcome_channel_id=1,
            welcome_message="안녕 {name}! 오늘은 {unknown_key}이야.",
        )
        dec = pol.decide("테스트")
        # KeyError 발생 시 원본 그대로여야 함
        assert dec.welcome_text == "안녕 {name}! 오늘은 {unknown_key}이야."

    def test_decide_returns_onboarding_decision(self):
        """반환 타입은 OnboardingDecision이다."""
        pol = OnboardingPolicy(welcome_channel_id=1)
        dec = pol.decide("u")
        assert isinstance(dec, OnboardingDecision)


# ---------------------------------------------------------------------------
# Fake 헬퍼 — on_member_join 어댑터 테스트용
# ---------------------------------------------------------------------------


class _FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id


class _FakeGuild:
    def __init__(self, role: _FakeRole | None = None) -> None:
        self._role = role

    def get_role(self, role_id: int):
        if self._role and self._role.id == role_id:
            return self._role
        return None


class _FakeMember:
    def __init__(self, display_name: str, guild: _FakeGuild | None = None) -> None:
        self.display_name = display_name
        self.guild = guild
        self.added_roles: list[_FakeRole] = []

    async def add_roles(self, role, *, reason: str = "") -> None:
        self.added_roles.append(role)


class _FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


def _make_adapter(policy) -> tuple[PycordAdapter, dict]:
    """PycordAdapter 인스턴스를 생성하고 fake 클라이언트로 교체한다."""
    adapter = PycordAdapter(
        token="fake-token",
        bot_name="지우",
        onboarding_policy=policy,
    )
    # _client.get_channel 을 fake로 교체 — 실제 discord 연결 없음
    fake_client_state: dict = {"channels": {}}

    def get_channel(channel_id: int):
        return fake_client_state["channels"].get(channel_id)

    adapter._client = types.SimpleNamespace(get_channel=get_channel)
    return adapter, fake_client_state


# ---------------------------------------------------------------------------
# PycordAdapter.on_member_join 통합 테스트 (오프라인)
# ---------------------------------------------------------------------------


class TestAdapterOnMemberJoin:
    def test_sends_welcome_and_assigns_role(self, loop):
        """welcome_channel_id + default_role_id 모두 설정 → 채널 전송 + 역할 부여."""
        pol = OnboardingPolicy(
            welcome_channel_id=50,
            welcome_message="어서 와, {name}!",
            default_role_id=77,
        )
        adapter, state = _make_adapter(pol)
        channel = _FakeChannel(50)
        state["channels"][50] = channel
        role = _FakeRole(77)
        member = _FakeMember("길동", guild=_FakeGuild(role=role))

        _run(adapter.on_member_join(member), loop)

        assert channel.sent == ["어서 와, 길동!"]
        assert member.added_roles == [role]

    def test_no_welcome_channel_skips_send(self, loop):
        """welcome_channel_id 미설정 → 채널 전송 없음."""
        pol = OnboardingPolicy(
            welcome_channel_id=None,
            default_role_id=77,
        )
        adapter, state = _make_adapter(pol)
        channel = _FakeChannel(50)
        state["channels"][50] = channel
        role = _FakeRole(77)
        member = _FakeMember("철수", guild=_FakeGuild(role=role))

        _run(adapter.on_member_join(member), loop)

        assert channel.sent == []
        assert member.added_roles == [role]

    def test_no_default_role_skips_role_assign(self, loop):
        """default_role_id 미설정 → 역할 부여 없음, 메시지는 전송."""
        pol = OnboardingPolicy(
            welcome_channel_id=50,
            welcome_message="안녕!",
            default_role_id=None,
        )
        adapter, state = _make_adapter(pol)
        channel = _FakeChannel(50)
        state["channels"][50] = channel
        member = _FakeMember("영희", guild=_FakeGuild())

        _run(adapter.on_member_join(member), loop)

        assert channel.sent == ["안녕!"]
        assert member.added_roles == []

    def test_both_unset_is_noop(self, loop):
        """welcome_channel_id + default_role_id 모두 미설정 → 아무 동작 없음."""
        pol = OnboardingPolicy()
        adapter, state = _make_adapter(pol)
        channel = _FakeChannel(50)
        state["channels"][50] = channel
        member = _FakeMember("유저", guild=_FakeGuild())

        _run(adapter.on_member_join(member), loop)

        assert channel.sent == []
        assert member.added_roles == []

    def test_no_onboarding_policy_returns_early(self, loop):
        """onboarding_policy=None → on_member_join이 즉시 반환된다(예외 없음)."""
        adapter, state = _make_adapter(None)
        member = _FakeMember("유저")

        # 예외 없이 실행돼야 한다.
        _run(adapter.on_member_join(member), loop)

    def test_exception_is_silenced(self, loop):
        """on_member_join 내부 예외가 봇을 죽이지 않는다(침묵 degrade)."""

        class _BombPolicy:
            def decide(self, name: str):
                raise RuntimeError("의도적 폭발")

        adapter, _ = _make_adapter(_BombPolicy())
        member = _FakeMember("유저")

        # RuntimeError가 밖으로 전파되지 않아야 한다.
        _run(adapter.on_member_join(member), loop)

    def test_channel_not_found_is_graceful(self, loop):
        """welcome 채널이 캐시에 없어도 예외 없이 처리된다."""
        pol = OnboardingPolicy(welcome_channel_id=99, welcome_message="안녕!")
        adapter, state = _make_adapter(pol)
        # 채널을 state에 등록하지 않음 → get_channel returns None
        member = _FakeMember("유저")

        _run(adapter.on_member_join(member), loop)  # 예외 없어야 함

    def test_role_not_found_is_graceful(self, loop):
        """default_role이 길드에 없어도 예외 없이 처리된다."""
        pol = OnboardingPolicy(default_role_id=99)
        adapter, _ = _make_adapter(pol)
        # 역할 없는 길드
        member = _FakeMember("유저", guild=_FakeGuild(role=None))

        _run(adapter.on_member_join(member), loop)  # 예외 없어야 함
        assert member.added_roles == []


class TestOnboardingRobustness:
    """S6 architect 수정 가드: 봇 스킵 + 환영/역할 독립 degrade."""

    def test_bot_member_is_not_onboarded(self, loop):
        pol = OnboardingPolicy(
            welcome_channel_id=50, welcome_message="어서 와, {name}!", default_role_id=77
        )
        adapter, state = _make_adapter(pol)
        channel = _FakeChannel(50)
        state["channels"][50] = channel
        role = _FakeRole(77)
        member = _FakeMember("봇계정", guild=_FakeGuild(role=role))
        member.bot = True  # 봇 입장

        _run(adapter.on_member_join(member), loop)

        assert channel.sent == []  # 봇에는 환영 안 함
        assert member.added_roles == []  # 봇에는 역할 안 줌

    def test_welcome_send_failure_does_not_block_role_assign(self, loop):
        # 환영 전송 실패(예: Send Messages 권한 부재)가 자동 역할 부여를 막지 않아야 한다.
        class _BoomChannel:
            def __init__(self, cid):
                self.id = cid

            async def send(self, text):
                raise RuntimeError("send 권한 없음")

        pol = OnboardingPolicy(
            welcome_channel_id=50, welcome_message="어서 와, {name}!", default_role_id=77
        )
        adapter, state = _make_adapter(pol)
        state["channels"][50] = _BoomChannel(50)
        role = _FakeRole(77)
        member = _FakeMember("길동", guild=_FakeGuild(role=role))

        _run(adapter.on_member_join(member), loop)  # 예외 전파 없이 침묵 degrade

        assert member.added_roles == [role]  # 환영 실패에도 역할은 독립적으로 부여됨
