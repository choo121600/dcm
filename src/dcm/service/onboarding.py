"""S6 onboarding policy — discord-free.

Decides whether to send a welcome message and whether to auto-assign a role when a new member joins.
Since it handles only pure Python data with no discord dependency, unit tests can run offline.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..i18n import t


@dataclass(frozen=True)
class OnboardingDecision:
    """Return value of decide(). If channel/role is None, that action is skipped."""

    welcome_text: str | None
    welcome_channel_id: int | None
    default_role_id: int | None


class OnboardingPolicy:
    """Holds onboarding settings and produces join decisions.

    Args:
        welcome_channel_id: Channel ID to send the welcome message to. None skips the message.
        welcome_message:    Welcome text template. The `{name}` placeholder is replaced with the member's display name.
        default_role_id:    Role ID to auto-assign to new members. None skips.
    """

    def __init__(
        self,
        *,
        settings=None,
        welcome_channel_id: int | None = None,
        welcome_message: str = "",  # empty → locale default (i18n onboarding.welcome_default)
        default_role_id: int | None = None,
    ) -> None:
        self._settings = settings  # GuildSettingsStore (multi-guild); None uses the global values (backward compatible)
        self._welcome_channel_id = welcome_channel_id
        self._welcome_message = welcome_message
        self._default_role_id = default_role_id

    def decide(
        self, member_display_name: str, guild_id: int | str | None = None
    ) -> OnboardingDecision:
        """Decides the action to take on join. Multi-guild: if settings + guild_id are present, use that
        guild's settings; otherwise use the constructor-injected global values (backward compatible).

        If a welcome channel is set, produce welcome_text (skip otherwise). If default_role is set, include the role.
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
            # Fall back to the active locale's default greeting when none is configured (§10).
            message = message or t("onboarding.welcome_default")
            # Safely substitute only the {name} placeholder. On format error, safely fall back to the original template.
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
