from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol


@dataclass
class IncomingMessage:
    """A message that mentions the bot (mention tokens already stripped from `content`)."""

    channel_id: str
    author_id: str
    author_name: str
    content: str
    # Caller's Discord role ids, resolved to plain data at the adapter boundary (ralplan S2).
    # Forward seam: the S3 NL-router chokepoint will check these against admin_role_id; the
    # slash-command chokepoint reads the interaction's roles directly.
    role_ids: frozenset[int] = frozenset()
    # 길드 주인은 항상 관리 권한 보유(디스코드 owner는 본래 모든 권한). 어댑터에서 판정해 채움.
    is_owner: bool = False


@dataclass
class BufferedMessage:
    """One line of recent channel history, for short-term context."""

    author_name: str
    content: str
    is_bot: bool

# --- Authz boundary contracts (ralplan S2) ---


@dataclass(frozen=True)
class AuthContext:
    """Plain-data authorization context resolved at the platform boundary (ralplan S2).

    The adapter (which holds discord types) resolves the caller's Discord role ids into this
    discord-free contract. The single privileged-dispatch chokepoint checks membership against
    the configured admin_role_id — authz is never inferred from message text, and the
    service/router layer stays discord-free.
    """

    author_id: str
    author_name: str
    role_ids: frozenset[int] = frozenset()
    is_owner: bool = False  # 길드 주인 여부 — True면 역할과 무관하게 관리자 (디스코드 owner = 모든 권한)


@dataclass(frozen=True)
class TargetRef:
    """A resolved action target carried as plain data (ralplan S2 seam, used by S5 confirm).

    The adapter resolves an opaque Discord id into a human-readable label so confirm prompts
    can show ``#general`` instead of an opaque id, without the service importing discord.
    """

    id: str
    label: str


def is_admin(role_ids: frozenset[int], admin_role_id: int, is_owner: bool = False) -> bool:
    """InvokerCheck predicate (ralplan S2): authorized iff the caller is the guild owner
    (Discord owners inherently hold every permission) OR holds the configured admin role.
    admin_role_id <= 0 (unset) still denies non-owners by construction."""
    if is_owner:
        return True
    return admin_role_id > 0 and admin_role_id in role_ids


# Returns the reply text to send, or None to stay silent.
MentionHandler = Callable[[IncomingMessage, list["BufferedMessage"]], Awaitable["str | None"]]


class ChatPlatform(Protocol):
    """Isolation boundary for the chat library (DESIGN.md §3).

    Only implementations of this Protocol import `discord`. The orchestrator and memory
    engine depend on this interface, so swapping the library (or platform) is local to here.
    """

    def on_mention(self, handler: MentionHandler) -> None: ...

    async def send(self, channel_id: str, text: str) -> None: ...

    async def recent_messages(self, channel_id: str, n: int) -> list[BufferedMessage]: ...

    async def run(self) -> None: ...


class GuildAdmin(Protocol):
    """Isolation boundary for guild-management primitives (ralplan S2).

    Only the platform adapter implements this; the service/command layer depends on this
    Protocol (DIP) and never imports discord. Members are addressed by id (no privileged
    members intent — the adapter resolves interaction-provided members with a fetch_member
    fallback). Every mutating op takes an audit ``reason`` (stamped as X-Audit-Log-Reason).
    Channel/role ids are returned as strings for cross-boundary plain-data.
    """

    async def create_category(self, guild_id: int, name: str, *, reason: str) -> str: ...

    async def create_channel(
        self, guild_id: int, name: str, kind: str, category_id: int | None = None, *, reason: str
    ) -> str: ...

    async def edit_channel(
        self,
        guild_id: int,
        channel_id: int,
        *,
        name: str | None = None,
        category_id: int | None = None,
        reason: str,
    ) -> None: ...

    async def delete_channel(self, guild_id: int, channel_id: int, *, reason: str) -> None: ...

    async def create_role(
        self, guild_id: int, name: str, *, permissions: int = 0, reason: str
    ) -> str: ...

    async def role_permissions(self, guild_id: int, role_id: int) -> int: ...

    async def assign_role(
        self, guild_id: int, user_id: int, role_id: int, *, reason: str
    ) -> None: ...

    async def remove_role(
        self, guild_id: int, user_id: int, role_id: int, *, reason: str
    ) -> None: ...

    async def set_role_permissions(
        self, guild_id: int, role_id: int, permissions: int, *, reason: str
    ) -> None: ...

    async def set_channel_role_overwrite(
        self, guild_id: int, channel_id: int, role_id: int, *, view: bool, reason: str
    ) -> None: ...
    async def kick_member(self, guild_id: int, user_id: int, *, reason: str) -> None: ...

    async def ban_member(self, guild_id: int, user_id: int, *, reason: str) -> None: ...

    async def timeout_member(
        self, guild_id: int, user_id: int, duration_seconds: int, *, reason: str
    ) -> None: ...

    async def purge_messages(
        self, guild_id: int, channel_id: int, count: int, *, reason: str
    ) -> int: ...

    # 읽기 전용 — 템플릿 적용의 멱등성/이름 해석에 사용 (audit reason 불필요).
    async def list_roles(self, guild_id: int) -> list[dict]: ...

    async def list_channels(self, guild_id: int) -> list[dict]: ...
