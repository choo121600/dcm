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
        welcome_channel_id: int | None = None,
        welcome_message: str = "환영합니다! 편하게 인사하고 대화해요 :)",
        default_role_id: int | None = None,
    ) -> None:
        self._welcome_channel_id = welcome_channel_id
        self._welcome_message = welcome_message
        self._default_role_id = default_role_id

    def decide(self, member_display_name: str) -> OnboardingDecision:
        """입장 시 취할 동작을 결정한다.

        welcome_channel_id가 설정돼 있으면 welcome_text를 생성하고 채널 ID를 포함한다.
        설정되지 않으면 메시지 전송을 스킵한다(welcome_text=None, welcome_channel_id=None).
        default_role_id가 설정돼 있으면 해당 역할 ID를 포함한다. 없으면 None.
        """
        if self._welcome_channel_id is not None:
            # {name} 플레이스홀더만 안전하게 치환한다. 다른 중괄호 패턴은 그대로 둔다.
            try:
                text: str | None = self._welcome_message.format(name=member_display_name)
            except Exception:
                # 어떤 포맷 오류(미지 플레이스홀더·불균형 중괄호·속성/인덱스 접근 등)도
                # 입장 처리를 죽이지 않도록 원본 템플릿으로 안전 fallback한다.
                text = self._welcome_message
            channel_id: int | None = self._welcome_channel_id
        else:
            text = None
            channel_id = None

        return OnboardingDecision(
            welcome_text=text,
            welcome_channel_id=channel_id,
            default_role_id=self._default_role_id,
        )
