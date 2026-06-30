"""S6 온보딩 정책 — discord-free.

신규 멤버 입장 시 welcome 메시지 전송 여부와 자동 역할 부여 여부를 결정한다.
discord 의존 없이 순수 Python 데이터만 다루므로 단위 테스트가 오프라인으로 가능하다.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OnboardingDecision:
    """decide() 반환값. 채널/역할이 None이면 해당 동작을 스킵한다."""

    welcome_text: str | None
    welcome_channel_id: int | None
    default_role_id: int | None


class OnboardingPolicy:
    """온보딩 설정 보유 및 입장 결정 생성.

    Args:
        welcome_channel_id: 웰컴 메시지를 보낼 채널 ID. None이면 메시지 스킵.
        welcome_message:    웰컴 텍스트 템플릿. `{name}` 플레이스홀더를 멤버 표시 이름으로 치환한다.
        default_role_id:    신규 멤버에게 자동 부여할 역할 ID. None이면 스킵.
    """

    def __init__(
        self,
        *,
        settings=None,
        welcome_channel_id: int | None = None,
        welcome_message: str = "환영합니다! 편하게 인사하고 대화해요 :)",
        default_role_id: int | None = None,
    ) -> None:
        self._settings = settings  # GuildSettingsStore (멀티길드); None이면 전역값 사용(하위호환)
        self._welcome_channel_id = welcome_channel_id
        self._welcome_message = welcome_message
        self._default_role_id = default_role_id

    def decide(
        self, member_display_name: str, guild_id: int | str | None = None
    ) -> OnboardingDecision:
        """입장 시 취할 동작을 결정한다. 멀티길드: settings + guild_id 가 있으면 그 길드 설정을,
        없으면 생성자 주입 전역값(하위호환)을 사용한다.

        welcome 채널이 설정돼 있으면 welcome_text 생성(없으면 스킵). default_role 설정 시 역할 포함.
        """
        channel_id = self._welcome_channel_id
        message = self._welcome_message
        role_id = self._default_role_id
        if self._settings is not None and guild_id is not None:
            s = self._settings.get(guild_id)
            channel_id = s.welcome_channel_id
            message = s.welcome_message or self._welcome_message
            role_id = s.default_role_id

        if channel_id is not None:
            # {name} 플레이스홀더만 안전 치환. 포맷 오류는 원본 템플릿으로 안전 fallback.
            try:
                text: str | None = message.format(name=member_display_name)
            except Exception:
                text = message
        else:
            text = None
            channel_id = None

        return OnboardingDecision(
            welcome_text=text,
            welcome_channel_id=channel_id,
            default_role_id=role_id,
        )
