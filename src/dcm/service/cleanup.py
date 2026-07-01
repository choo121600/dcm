"""'Plan' for archiving inactive channels + deleting orphan roles (discord-free, pure data).

Two-stage lifecycle:
  1) Archive: move inactive text channels into the '📦 아카이브' category (+ hide from members). Reversible.
  2) Purge  : delete every channel inside '📦 아카이브' + delete orphan roles. Irreversible.
Only decides *what* to do on top of the channel/role dicts the adapter passes in; the actual Discord
calls (move/create/delete/permissions) are done by GuildAdminService. Being pure functions, it is easy
to unit-test.

Design decisions (agreed with the user):
- Inactivity criterion = days since last message ≥ N days (default 90). A channel with no messages is
  also treated as inactive.
- The primary archive targets are text channels (type 0). Voice (2)/stage (13) are co-archived only
  when they form a 'name pair' with a dead text channel
  (e.g. chess-engine-algo-채팅 ↔ CHESS-ENGINE-ALGO-음성) — a plain voice lounge with no text
  counterpart may be call-only-active, so it is left untouched. Forum (15)/announcement (5) excluded.
- Channels already inside '📦 아카이브' are classified as purge (delete) targets (not archived again).
- A role is a deletion candidate only if it has 0 members + is not a bot/integration + is not
  @everyone/an admin role/a protected role + is unused by any live (non-archive/purge) channel. Role
  deletion happens only in the purge stage.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..i18n import t

DISCORD_EPOCH_MS = 1420070400000
DEFAULT_INACTIVE_DAYS = 90

# Archive category name. When it exceeds 50, split into "📦 아카이브 2", "📦 아카이브 3" … (per-category limit).
ARCHIVE_CATEGORY_BASE = "📦 아카이브"
MAX_CHANNELS_PER_CATEGORY = 50

# Channel types considered as archive candidates: text only.
ARCHIVABLE_TYPES = frozenset({0})

# Co-archive types: voice (2)/stage (13). Archived together only when they form a 'name pair' with a dead text channel.
CO_ARCHIVE_TYPES = frozenset({2, 13})

# Channel-name fragments always excluded from archiving (ops/notice/entry, etc.). Case-insensitive
# substring match — since the operator reviews the dry-run, we cast a conservatively wide net.
PROTECTED_NAME_PARTS = (
    "공지", "announce", "입구", "welcome", "환영", "규칙", "rule",
    "역할", "role", "관리", "운영", "moderator", "admin", "봇", "bot", "보관", "archive", "아카이브",
)


@dataclass(frozen=True)
class ChannelAction:
    id: int
    name: str
    age_days: float | None  # None = no message trace


@dataclass(frozen=True)
class RoleAction:
    id: int
    name: str
    reason: str


@dataclass
class CleanupPlan:
    inactive_days: int
    archive_channels: list[ChannelAction] = field(default_factory=list)  # → move to archive (reversible)
    purge_channels: list[ChannelAction] = field(default_factory=list)  # already in archive → delete (permanent)
    delete_roles: list[RoleAction] = field(default_factory=list)  # orphan roles → delete (permanent)
    orphan_categories: list[ChannelAction] = field(default_factory=list)  # empty (orphan) categories → delete (permanent)
    skipped_protected: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (
            self.archive_channels or self.purge_channels or self.delete_roles or self.orphan_categories
        )

    def _chan_line(self, c: ChannelAction) -> str:
        age = t("cleanup.no_messages") if c.age_days is None else t("cleanup.days_ago", days=c.age_days)
        return t("cleanup.chan_line", name=c.name, age=age)

    def summary(self) -> str:
        """Full summary for the report (to-archive + to-purge + roles)."""
        lines = [t("cleanup.report_header", days=self.inactive_days)]
        lines.append(t("cleanup.report_archive_section", count=len(self.archive_channels)))
        for c in self.archive_channels[:20]:
            lines.append(self._chan_line(c))
        if len(self.archive_channels) > 20:
            lines.append(t("cleanup.report_more", count=len(self.archive_channels) - 20))
        lines.append(t("cleanup.report_purge_section", count=len(self.purge_channels)))
        for c in self.purge_channels[:20]:
            lines.append(t("cleanup.chan_bullet", name=c.name))
        if len(self.purge_channels) > 20:
            lines.append(t("cleanup.report_more", count=len(self.purge_channels) - 20))
        lines.append(t("cleanup.report_roles_section", count=len(self.delete_roles)))
        for r in self.delete_roles[:20]:
            lines.append(t("cleanup.role_bullet", name=r.name, reason=r.reason))
        if len(self.delete_roles) > 20:
            lines.append(t("cleanup.report_more", count=len(self.delete_roles) - 20))
        lines.append(t("cleanup.report_categories_section", count=len(self.orphan_categories)))
        for c in self.orphan_categories[:20]:
            lines.append(t("cleanup.cat_bullet", name=c.name))
        if len(self.orphan_categories) > 20:
            lines.append(t("cleanup.report_more", count=len(self.orphan_categories) - 20))
        lines.append(t("cleanup.report_commands"))
        return "\n".join(lines)

    def archive_summary(self) -> str:
        if not self.archive_channels:
            return t("cleanup.archive_none")
        lines = [t("cleanup.archive_header", count=len(self.archive_channels))]
        for c in self.archive_channels[:30]:
            lines.append(self._chan_line(c))
        if len(self.archive_channels) > 30:
            lines.append(t("cleanup.report_more", count=len(self.archive_channels) - 30))
        return "\n".join(lines)

    def purge_summary(self) -> str:
        if not self.purge_channels and not self.delete_roles and not self.orphan_categories:
            return t("cleanup.purge_none")
        lines = [t("cleanup.purge_header")]
        if self.purge_channels:
            lines.append(t("cleanup.purge_channels_section", count=len(self.purge_channels)))
            for c in self.purge_channels[:30]:
                lines.append(t("cleanup.chan_bullet", name=c.name))
            if len(self.purge_channels) > 30:
                lines.append(t("cleanup.report_more", count=len(self.purge_channels) - 30))
        if self.delete_roles:
            lines.append(t("cleanup.purge_roles_section", count=len(self.delete_roles)))
            for r in self.delete_roles[:30]:
                lines.append(t("cleanup.role_bullet", name=r.name, reason=r.reason))
            if len(self.delete_roles) > 30:
                lines.append(t("cleanup.report_more", count=len(self.delete_roles) - 30))
        if self.orphan_categories:
            lines.append(t("cleanup.purge_categories_section", count=len(self.orphan_categories)))
            for c in self.orphan_categories[:30]:
                lines.append(t("cleanup.cat_bullet", name=c.name))
            if len(self.orphan_categories) > 30:
                lines.append(t("cleanup.report_more", count=len(self.orphan_categories) - 30))
        return "\n".join(lines)


def age_days(last_message_id, now_ms: float) -> float | None:
    """Days since last activity from the Discord snowflake (last_message_id). None if absent."""
    if not last_message_id:
        return None
    ts = (int(last_message_id) >> 22) + DISCORD_EPOCH_MS
    return (now_ms - ts) / 86400000.0


def _is_protected(name: str, protected_parts) -> bool:
    low = (name or "").lower()
    return any(p.lower() in low for p in protected_parts)


# Season keywords for detecting cohort roles — if a name contains one of these, treat it as a cohort and preserve.
SEASON_KEYWORDS = ("summer", "winter", "spring", "fall", "autumn", "여름", "겨울", "봄", "가을")


def _is_cohort(name: str) -> bool:
    """True only for umbrella cohorts (preserve): names made up of a season keyword + year/separators
    only (e.g. '2023 Summer', '25-SUMMER'). A detail with a project name after the season
    (e.g. 25-SUMMER-DJANGO) is merged into 25-SUMMER, so it is not a cohort → a cleanup target."""
    low = (name or "").lower()
    if not any(k in low for k in SEASON_KEYWORDS):
        return False
    rest = low
    for k in SEASON_KEYWORDS:
        rest = rest.replace(k, "")
    # If no letters (project name) remain after removing the season, it is an umbrella cohort.
    return not any(ch.isalpha() for ch in rest)


def _base_name(name: str) -> str:
    """Strip the kind suffix (음성/voice/채팅/chat/채널) from a channel name and lowercase it — for text↔voice name-pair matching."""
    s = (name or "").lower().strip()
    for suf in ("-음성", " 음성", "음성", "-voice", "voice", "-채팅", " 채팅", "채팅", "-chat", "chat", "-채널", "채널"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s.strip(" -_·")


def find_archive_category_ids(channels: list[dict]) -> set[int]:
    """Set of ids for the '📦 아카이브' category (and its splits)."""
    return {
        int(c["id"])
        for c in channels
        if int(c.get("type", -1)) == 4 and (c.get("name") or "").startswith(ARCHIVE_CATEGORY_BASE)
    }


def plan_cleanup(
    channels: list[dict],
    roles: list[dict],
    *,
    now_ms: float,
    inactive_days: int = DEFAULT_INACTIVE_DAYS,
    admin_role_id: int = 0,
    welcome_channel_id: int = 0,
    protected_parts=PROTECTED_NAME_PARTS,
    protected_role_ids=(),
) -> CleanupPlan:
    """Build a cleanup plan on top of the channel/role dicts (pure function, no Discord calls).

    Expected channel dict keys: id, name, type(int), parent_id(str|None), last_message_id(str|None),
                                overwrite_role_ids(list[str]).
    Expected role dict keys: id, name, member_count(int), managed(bool), is_default(bool).
    """
    plan = CleanupPlan(inactive_days=inactive_days)
    archive_cat_ids = find_archive_category_ids(channels)

    for c in channels:
        cid = int(c["id"])
        parent = c.get("parent_id")
        # Already inside archive → purge (delete) target; not archived again.
        if parent and int(parent) in archive_cat_ids:
            plan.purge_channels.append(
                ChannelAction(cid, c.get("name", ""), age_days(c.get("last_message_id"), now_ms))
            )
            continue
        if int(c.get("type", -1)) not in ARCHIVABLE_TYPES:
            continue
        name = c.get("name", "")
        if welcome_channel_id and cid == int(welcome_channel_id):
            continue
        if _is_protected(name, protected_parts):
            plan.skipped_protected.append(name)
            continue
        a = age_days(c.get("last_message_id"), now_ms)
        if a is None or a >= inactive_days:
            plan.archive_channels.append(ChannelAction(cid, name, a))

    # Archive voice/stage too — unless there is recent (< threshold) activity: (1) the voice-text chat is old,
    # (2) it forms a name pair with a dead text channel, or (3) it belongs to a 'dead category' with no
    # recently-active channel at all (including text-less topic lounges). A text-less voice channel in a
    # category that has an active channel is preserved.
    archived_text_bases = {_base_name(ca.name) for ca in plan.archive_channels}
    active_categories: set[int] = set()
    for c in channels:
        p = c.get("parent_id")
        if not p:
            continue
        a = age_days(c.get("last_message_id"), now_ms)
        if a is not None and a < inactive_days:
            active_categories.add(int(p))
    for c in channels:
        if int(c.get("type", -1)) not in CO_ARCHIVE_TYPES:
            continue
        cid = int(c["id"])
        parent = c.get("parent_id")
        if parent and int(parent) in archive_cat_ids:
            continue
        name = c.get("name", "")
        if _is_protected(name, protected_parts):
            continue
        a = age_days(c.get("last_message_id"), now_ms)
        if a is not None and a < inactive_days:
            continue  # recent voice-text chat activity → keep
        if (
            a is not None
            or _base_name(name) in archived_text_bases
            or (parent and int(parent) not in active_categories)
        ):
            plan.archive_channels.append(ChannelAction(cid, name, a))

    # Role classification: a role used by a live channel is preserved. A role 'dedicated' to a dead
    # (archive/purge) channel is cleaned up — even if it still has members, it is useless once the channel
    # is gone. An identity role unrelated to channels (unused anywhere + has members) is preserved
    # (interests/cohort/color, etc.).
    moving_or_purging = {ca.id for ca in plan.archive_channels} | {cp.id for cp in plan.purge_channels}
    live_role_refs: set[int] = set()
    dead_role_refs: set[int] = set()
    for c in channels:
        cid = int(c["id"])
        for rid in c.get("overwrite_role_ids") or []:
            (dead_role_refs if cid in moving_or_purging else live_role_refs).add(int(rid))

    protected_ids = {int(x) for x in (protected_role_ids or ())}
    for r in roles:
        if r.get("is_default") or r.get("managed"):
            continue
        rid = int(r["id"])
        name = r.get("name", str(rid))
        if admin_role_id and rid == int(admin_role_id):
            continue
        if rid in protected_ids:
            continue  # externally protected role (e.g. level reward)
        if _is_protected(name, protected_parts):
            continue  # name-protected — staff roles like ops/admin/mod
        if rid in live_role_refs:
            continue  # a role used by a live channel is preserved
        mc = int(r.get("member_count", 0))
        if rid in dead_role_refs:
            # role dedicated to a dead channel — cleaned up even with members (useless once the channel is gone).
            plan.delete_roles.append(RoleAction(rid, name, t("cleanup.reason_dead_channel", mc=mc)))
        elif mc == 0:
            plan.delete_roles.append(RoleAction(rid, name, t("cleanup.reason_no_members")))
        elif not _is_cohort(name):
            # unused by channels + has members + not a cohort (season name) → removal candidate (user policy).
            plan.delete_roles.append(RoleAction(rid, name, t("cleanup.reason_unused_member", mc=mc)))
        # else: cohort (season name) role → preserve

    # Orphan (empty) categories: categories with 0 child channels. Archive categories and protected names are excluded.
    child_counts: dict[int, int] = {}
    for c in channels:
        p = c.get("parent_id")
        if p:
            child_counts[int(p)] = child_counts.get(int(p), 0) + 1
    for c in channels:
        if int(c.get("type", -1)) != 4:
            continue
        cid = int(c["id"])
        if cid in archive_cat_ids or _is_protected(c.get("name", ""), protected_parts):
            continue
        if child_counts.get(cid, 0) == 0:
            plan.orphan_categories.append(ChannelAction(cid, c.get("name", ""), None))

    return plan
