"""LevelingService — hot-path XP accrual + display (embed) + (gating/roles in G003/G004).

record_message is called on the on_message hot path. To keep 0 hot-path DB READs (steady state):
- Cooldown is a first-stage gate via an in-memory per-(guild,user) dict (P1/F6).
- Per-guild settings (leveling_enabled/cooldown/top_n) are held in a read-through TTL cache, so
  on a cache hit no guild_settings SQLite read happens (only a cache miss triggers one read per
  TTL).
Any failure degrades silently (0 impact on the mention path or bot behavior). The cooldown dict
periodically sweeps stale entries to prevent unbounded growth.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable

import discord

from ..i18n import t
from .scoring import (
    caps_ratio,
    danger_score,
    level,
    penalty_weight,
    progress,
    utc_day,
    xp_award,
    xp_to_next,
)
from .store import LevelingStore

log = logging.getLogger(__name__)

DEFAULT_COOLDOWN_SECONDS = 60.0
DEFAULT_TOP_N = 10
SETTINGS_CACHE_TTL = 60.0  # per-guild settings cache lifetime (seconds, monotonic) — changes propagate within this time
_EVICT_THRESHOLD = 5_000  # sweep stale entries once _last_award exceeds this size
_EVICT_MAX_AGE = 3_600.0  # cooldown entries older than this are safe to remove (any cooldown has elapsed)
# trust-decay hot-path parameters (in-memory, code defaults).
FLOOD_WINDOW_SECONDS = 10.0  # sliding-window length for flood counting (seconds)
PENALTY_WINDOW_SECONDS = 60.0  # window length for the penalty-frequency cap (seconds)
PENALTY_WINDOW_CAP = 3  # max penalties within the window (guards against false positives / gaming)
PENALTY_DAILY_CAP = 20  # max penalties per day (UTC-day)
LEVEL_ROLE_HYSTERESIS = 1  # on demotion, revoke only 'below' reward_level minus this margin (dampens boundary flapping)
_EMBED_COLOR = 0x5865F2  # Discord blurple
# trust-gating quota tiers (code defaults): (min level, daily limit). Derived from level.
WEB_QUOTA_TIERS = ((0, 20), (5, 50), (10, 100))
LLM_QUOTA_TIERS = ((0, 100), (5, 300), (10, 600))
# explicit 'dangerous' permissions (denial reason labeled dangerous:*). Fail-closed together with the SAFE allow-list below.
DANGEROUS_PERMISSION_NAMES = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_permissions",
    "manage_channels",
    "manage_messages",
    "manage_webhooks",
    "manage_nicknames",
    "manage_threads",
    "manage_emojis_and_stickers",
    "manage_expressions",
    "manage_events",
    "kick_members",
    "ban_members",
    "moderate_members",
    "mute_members",
    "deafen_members",
    "move_members",
    "mention_everyone",
    "view_audit_log",
    "priority_speaker",
)

# fail-closed allow-list: an unattended auto-grant role is denied if it holds any permission
# outside the 'safe' set below. (Not a simple deny-list — unknown/future Discord permissions are
# auto-denied too unless present in SAFE.)
SAFE_PERMISSION_NAMES = frozenset(
    {
        "view_channel", "read_messages", "read_message_history",
        "send_messages", "send_messages_in_threads",
        "create_public_threads", "create_private_threads",
        "add_reactions", "embed_links", "attach_files",
        "external_emojis", "use_external_emojis",
        "external_stickers", "use_external_stickers",
        "use_application_commands", "use_slash_commands",
        "change_nickname",
        "connect", "speak", "stream", "use_voice_activation",
        "request_to_speak", "use_embedded_activities", "send_voice_messages",
    }
)


def _all_permission_names() -> set[str]:
    """Permission names to check = explicit dangerous ones + (if available) all Discord flags (ensures fail-closed)."""
    names = set(DANGEROUS_PERMISSION_NAMES)
    valid = getattr(discord.Permissions, "VALID_FLAGS", None)
    if isinstance(valid, dict):
        names |= set(valid.keys())
    return names


class LevelingService:
    def __init__(
        self,
        store: LevelingStore,
        settings_store=None,
        *,
        default_cooldown: float = DEFAULT_COOLDOWN_SECONDS,
        default_top_n: int = DEFAULT_TOP_N,
    ) -> None:
        self._store = store
        self._settings = settings_store
        self._default_cooldown = float(default_cooldown)
        self._default_top_n = int(default_top_n)
        # in-memory cooldown: (guild_id, user_id) -> monotonic time of the last award.
        self._last_award: dict[tuple[str, str], float] = {}
        # per-guild settings read-through cache: guild_id -> (fetched_monotonic, settings|None).
        self._settings_cache: dict[str, tuple[float, object]] = {}
        # trust-decay in-memory counters — keep 0 hot-path DB reads (assumes a single event-loop thread).
        self._flood: dict[tuple[str, str], deque] = {}  # sliding window of message timestamps
        self._penalty_window: dict[tuple[str, str], deque] = {}  # window of penalty timestamps (cap)
        self._penalty_daily: dict[tuple[str, str, str], int] = {}  # (guild,user,utc_day)->count

    # --- per-guild settings (TTL cache + degrade to defaults on failure) ---

    def _cached_settings(self, guild_id, mono: float):
        if self._settings is None:
            return None
        key = str(guild_id)
        hit = self._settings_cache.get(key)
        if hit is not None and (mono - hit[0]) < SETTINGS_CACHE_TTL:
            return hit[1]
        try:
            value = self._settings.get(guild_id)
        except Exception:  # noqa: BLE001
            value = None
        self._settings_cache[key] = (mono, value)
        return value

    @staticmethod
    def _enabled(settings) -> bool:
        en = getattr(settings, "leveling_enabled", None) if settings is not None else None
        return True if en is None else bool(en)

    def _cooldown(self, settings) -> float:
        cd = getattr(settings, "leveling_cooldown_seconds", None) if settings is not None else None
        try:
            return float(cd) if cd else self._default_cooldown
        except (TypeError, ValueError):
            return self._default_cooldown

    def _top_n(self, settings) -> int:
        n = getattr(settings, "leveling_top_n", None) if settings is not None else None
        try:
            return int(n) if n else self._default_top_n
        except (TypeError, ValueError):
            return self._default_top_n

    @staticmethod
    def _decay_enabled(settings) -> bool:
        en = getattr(settings, "leveling_decay_enabled", None) if settings is not None else None
        return False if en is None else bool(en)  # default OFF (conservative rollout)

    @staticmethod
    def _decay_shadow(settings) -> bool:
        sh = getattr(settings, "leveling_decay_shadow", None) if settings is not None else None
        return False if sh is None else bool(sh)  # default enforce (actually deducts when decay is enabled)

    @staticmethod
    def _danger_enabled(settings) -> bool:
        v = getattr(settings, "leveling_danger_enabled", None) if settings is not None else None
        return False if v is None else bool(v)  # default OFF (danger wordlist disabled)

    @staticmethod
    def _injection_enabled(settings) -> bool:
        v = getattr(settings, "leveling_injection_enabled", None) if settings is not None else None
        return False if v is None else bool(v)  # default OFF (injection signal disabled)

    def _maybe_evict(self, mono: float) -> None:
        if (
            len(self._last_award) < _EVICT_THRESHOLD
            and len(self._flood) < _EVICT_THRESHOLD
            and len(self._penalty_window) < _EVICT_THRESHOLD
            and len(self._penalty_daily) < _EVICT_THRESHOLD
        ):
            return
        cutoff = mono - _EVICT_MAX_AGE
        self._last_award = {k: v for k, v in self._last_award.items() if v >= cutoff}
        # decay window deques: drop entries whose most recent activity is stale.
        self._flood = {k: dq for k, dq in self._flood.items() if dq and dq[-1] >= cutoff}
        self._penalty_window = {
            k: dq for k, dq in self._penalty_window.items() if dq and dq[-1] >= cutoff
        }
        # daily penalty counters: drop entries other than today (latest utc_day).
        if self._penalty_daily:
            today = max(day for _, _, day in self._penalty_daily)
            self._penalty_daily = {k: v for k, v in self._penalty_daily.items() if k[2] == today}

    # --- hot path ---

    def record_message(
        self,
        guild_id: int | str,
        user_id: int | str,
        text: str,
        *,
        now: float | None = None,
        monotonic_time: float | None = None,
        mention_count: int = 0,
        is_admin: bool = False,
        on_penalty: Callable[[], None] | None = None,
    ) -> bool:
        """Award quality-weighted XP for a non-bot message. In-memory cooldown as first-stage gate
        + settings TTL cache (0 hot-path DB READs).

        Returns: True if XP was awarded, False if skipped (disabled/cooldown/worthless).
        Exceptions degrade silently.
        """
        try:
            mono = monotonic_time if monotonic_time is not None else time.monotonic()
            settings = self._cached_settings(guild_id, mono)
            if not self._enabled(settings):
                return False
            key = (str(guild_id), str(user_id))
            # trust-decay: update the flood counter before the cooldown gate (in-memory, 0 DB reads).
            if not is_admin and self._decay_enabled(settings):
                flood_count = self._record_flood(key, mono)
                self._maybe_evict(mono)  # non-awarding traffic also triggers a stale sweep of the decay maps
                penalty = penalty_weight(text, flood_count, mention_count, caps_ratio(text))
                danger = danger_score(text) if self._danger_enabled(settings) else 0
                penalty += danger
                if penalty < 0:
                    signals = f"flood={flood_count} mentions={mention_count}" + (
                        " danger=1" if danger < 0 else ""
                    )
                    if self._apply_penalty(
                        guild_id, user_id, key, penalty, mono, now, settings, signals=signals
                    ):
                        if on_penalty is not None:
                            on_penalty()  # possible demotion -> trigger background reconcile (role revocation) (P2)
                        return False  # if the penalty is actually deducted, this message earns no XP
            last = self._last_award.get(key)
            if last is not None and mono - last < self._cooldown(settings):
                return False
            xp = xp_award(text)
            if xp <= 0:
                return False  # empty/worthless messages are skipped without consuming the cooldown
            self._last_award[key] = mono
            self._maybe_evict(mono)
            wall = now if now is not None else time.time()
            self._store.add_xp(guild_id, user_id, xp, now=wall)
            return True
        except Exception:  # noqa: BLE001
            log.exception("leveling record_message failed (silent degrade)")
            return False

    def _record_flood(self, key: tuple[str, str], mono: float) -> int:
        """Number of messages within the sliding window (FLOOD_WINDOW_SECONDS) — in-memory only (0 DB reads)."""
        dq = self._flood.get(key)
        if dq is None:
            dq = deque()
            self._flood[key] = dq
        dq.append(mono)
        cutoff = mono - FLOOD_WINDOW_SECONDS
        while dq and dq[0] < cutoff:
            dq.popleft()
        return len(dq)

    def _apply_penalty(
        self,
        guild_id: int | str,
        user_id: int | str,
        key: tuple[str, str],
        penalty: int,
        mono: float,
        now: float | None,
        settings,
        *,
        signals: str,
    ) -> bool:
        """Apply a trust-decay penalty. Window/daily cap and shadow guards. Returns True when a
        deduction actually happens (all audited).

        Returns True = weighted_xp is actually deducted (this message skips its award).
        False = not deducted due to shadow/cap. 0 DB reads — uses only an in-memory cap plus a
        fire-and-forget add_xp (write).
        """
        pw = self._penalty_window.get(key)
        if pw is None:
            pw = deque()
            self._penalty_window[key] = pw
        cutoff = mono - PENALTY_WINDOW_SECONDS
        while pw and pw[0] < cutoff:
            pw.popleft()
        if len(pw) >= PENALTY_WINDOW_CAP:
            return False  # window cap exceeded — no further deduction
        wall = now if now is not None else time.time()
        day = utc_day(wall)
        dkey = (key[0], key[1], day)
        if self._penalty_daily.get(dkey, 0) >= PENALTY_DAILY_CAP:
            return False  # daily cap exceeded

        if self._decay_shadow(settings):
            log.info(
                "leveling decay SHADOW (would-penalize): guild=%s user=%s delta=%s %s",
                key[0],
                key[1],
                penalty,
                signals,
            )
            return False
        pw.append(mono)
        self._penalty_daily[dkey] = self._penalty_daily.get(dkey, 0) + 1
        self._store.add_xp(guild_id, user_id, penalty, now=wall)
        log.warning(
            "leveling decay penalty: guild=%s user=%s delta=%s %s",
            key[0],
            key[1],
            penalty,
            signals,
        )
        return True

    def apply_signal_penalty(
        self,
        guild_id: int | str,
        user_id: int | str,
        penalty_xp: int,
        *,
        signal: str,
        now: float | None = None,
    ) -> bool:
        """Non-hot-path (e.g. ingest) signal-based trust-decay penalty (injection/danger).

        Only when decay + the per-signal toggle are enabled; shares the window/daily cap, shadow,
        and audit path via _apply_penalty. Not a hot path — settings cache read is allowed.
        Returns True = actually deducted.
        """
        try:
            if int(penalty_xp) >= 0:
                return False
            mono = time.monotonic()
            settings = self._cached_settings(guild_id, mono)
            if not self._decay_enabled(settings):
                return False
            if signal == "injection" and not self._injection_enabled(settings):
                return False
            if signal == "danger" and not self._danger_enabled(settings):
                return False
            key = (str(guild_id), str(user_id))
            return self._apply_penalty(
                guild_id,
                user_id,
                key,
                int(penalty_xp),
                mono,
                now,
                settings,
                signals=f"signal={signal}",
            )
        except Exception:  # noqa: BLE001
            log.exception("leveling apply_signal_penalty failed (silent)")
            return False

    # --- trust gating (G003/G004): per-level daily limits ---

    @staticmethod
    def _quota_limit(kind: str, lvl: int) -> int:
        tiers = WEB_QUOTA_TIERS if kind == "web" else LLM_QUOTA_TIERS
        limit = tiers[0][1]
        for min_lvl, lim in tiers:
            if lvl >= min_lvl:
                limit = lim
        return limit

    def quota_check(
        self, guild_id: int | str, user_id: int | str, kind: str, *, day: str | None = None
    ) -> tuple[bool, int]:
        """Check usage against the level-derived daily limit. Returns (allowed, remaining).

        allowed=True means within the limit. Exceptions / lookup failures degrade fail-open
        (allow) so a transient error doesn't block normal features (web has its own separate
        cooldown/cost guard).
        """
        try:
            xp, _ = self._store.get_record(guild_id, user_id)
            limit = self._quota_limit(kind, level(xp))
            d = day or utc_day(time.time())
            used = self._store.get_daily_usage(guild_id, user_id, d, kind)
            return (used < limit, max(0, limit - used))
        except Exception:  # noqa: BLE001
            log.exception("leveling quota_check failed (fail-open)")
            return (True, 0)

    def record_usage(
        self, guild_id: int | str, user_id: int | str, kind: str, *, day: str | None = None
    ) -> None:
        """Record one successful web/llm use in the daily usage counter (fails silently)."""
        try:
            d = day or utc_day(time.time())
            self._store.incr_daily_usage(guild_id, user_id, d, kind)
        except Exception:  # noqa: BLE001
            log.exception("leveling record_usage failed (silent)")

    # --- level-to-role rewards (G004): unattended allow-list grant ---

    @staticmethod
    def _role_grant_ok(role, guild, bot_top_position) -> tuple[bool, str]:
        """Safety check for unattended auto-grant (fail-closed allow-list): allow only decorative roles that pass the hierarchy check and hold 0 permissions outside SAFE."""
        if getattr(role, "managed", False):
            return (False, "managed")
        default_role = getattr(guild, "default_role", None)
        if default_role is not None and role == default_role:
            return (False, "everyone")
        if bot_top_position is None:
            return (False, "no-bot-top-role")
        if getattr(role, "position", 0) >= bot_top_position:
            return (False, "hierarchy")  # only strictly below the bot's top role
        perms = getattr(role, "permissions", None)
        if perms is not None:
            for name in _all_permission_names():
                if name in SAFE_PERMISSION_NAMES or not getattr(perms, name, False):
                    continue
                label = "dangerous" if name in DANGEROUS_PERMISSION_NAMES else "unsafe-perm"
                return (False, f"{label}:{name}")
        return (True, "ok")

    async def reconcile_roles(self, member) -> None:
        """Grant (on reaching a level) or revoke (on trust decay, P2) the mapped roles according
        to the member's level.

        Only reward roles that pass the allow-list guard (_role_grant_ok) are affected —
        onboarding default_role, managed, over-hierarchy, and dangerous-permission roles are
        structurally excluded. Idempotent; fails silently. Revocation happens only 'below'
        reward_level - LEVEL_ROLE_HYSTERESIS (dampens boundary flapping).
        """
        try:
            guild = getattr(member, "guild", None)
            if guild is None:
                return
            rewards = self._store.get_role_rewards(guild.id)
            if not rewards:
                return
            xp, _ = self._store.get_record(guild.id, member.id)
            lvl = level(xp)
            bot_member = getattr(guild, "me", None)
            bot_top = getattr(getattr(bot_member, "top_role", None), "position", None)
            member_roles = getattr(member, "roles", []) or []
            onboarding_id = self._onboarding_role_id(guild.id)
            for reward_level, role_id in rewards:
                role = guild.get_role(role_id) if hasattr(guild, "get_role") else None
                if role is None:
                    continue  # skip deleted role
                has_role = role in member_roles
                if lvl >= reward_level:
                    # grant: idempotently grant the role for the reached level
                    if has_role:
                        continue  # idempotent: already held
                    ok, reason = self._role_grant_ok(role, guild, bot_top)
                    if not ok:
                        log.warning(
                            "leveling role-reward denied: guild=%s role=%s reason=%s",
                            guild.id,
                            role_id,
                            reason,
                        )
                        continue
                    try:
                        await member.add_roles(
                            role, reason=f"auto-grant level>={reward_level} -> role {role_id}"
                        )
                        log.info(
                            "leveling auto-grant: guild=%s user=%s level>=%s role=%s",
                            guild.id,
                            member.id,
                            reward_level,
                            role_id,
                        )
                    except Exception:  # noqa: BLE001 (discord.Forbidden/HTTPException etc.)
                        log.exception("leveling add_roles failed (silent degrade)")
                elif lvl < reward_level - LEVEL_ROLE_HYSTERESIS and has_role:
                    # revoke (P2): fell below threshold due to trust decay → revoke only reward
                    # roles the bot could grant. Roles failing _role_grant_ok
                    # (managed/everyone/hierarchy/dangerous-permission) are preserved (not auto-revoked).
                    if onboarding_id is not None and role_id == onboarding_id:
                        continue  # never auto-revoke the onboarding role even if reward-mapped (preserves channel access)
                    ok, _reason = self._role_grant_ok(role, guild, bot_top)
                    if not ok:
                        continue
                    try:
                        await member.remove_roles(
                            role, reason=f"auto-revoke level<{reward_level} (trust decay)"
                        )
                        log.info(
                            "leveling auto-revoke: guild=%s user=%s level<%s role=%s",
                            guild.id,
                            member.id,
                            reward_level,
                            role_id,
                        )
                    except Exception:  # noqa: BLE001 (discord.Forbidden/HTTPException etc.)
                        log.exception("leveling remove_roles failed (silent degrade)")
        except Exception:  # noqa: BLE001
            log.exception("reconcile_roles failed (silent degrade)")

    # --- role reward configuration (admin) ---

    def _onboarding_role_id(self, guild_id) -> int | None:
        """The guild's onboarding (granted on join) default_role_id — used to exclude it from revocation/reward candidates."""
        settings = self._cached_settings(guild_id, time.monotonic())
        rid = getattr(settings, "default_role_id", None) if settings is not None else None
        try:
            return int(rid) if rid else None
        except (TypeError, ValueError):
            return None

    def validate_reward_role(self, role, guild) -> tuple[bool, str]:
        """Allow-list pre-validation at configuration time (grant-time guard + onboarding-role rejection)."""
        onboarding_id = self._onboarding_role_id(getattr(guild, "id", None))
        if onboarding_id is not None and getattr(role, "id", None) == onboarding_id:
            return (False, "onboarding-role")  # forbid mapping the onboarding role as a reward (auto-revoke footgun)
        bot_member = getattr(guild, "me", None)
        bot_top = getattr(getattr(bot_member, "top_role", None), "position", None)
        return self._role_grant_ok(role, guild, bot_top)

    def set_role_reward(self, guild_id: int | str, level: int, role_id: int) -> None:
        self._store.set_role_reward(guild_id, level, role_id)

    def remove_role_reward(self, guild_id: int | str, level: int) -> None:
        self._store.remove_role_reward(guild_id, level)

    def list_role_rewards(self, guild_id: int | str) -> list[tuple[int, int]]:
        return self._store.get_role_rewards(guild_id)

    # --- display (embed) ---

    def rank_embed(self, guild_id: int | str, user_id: int | str, display_name: str):
        xp, _ = self._store.get_record(guild_id, user_id)
        lvl = level(xp)
        prog = progress(xp)
        remaining = xp_to_next(xp)
        embed = discord.Embed(title=t("leveling.rank_title", display_name=display_name), color=_EMBED_COLOR)
        embed.add_field(name=t("leveling.field_level"), value=str(lvl), inline=True)
        embed.add_field(name=t("leveling.field_total_xp"), value=str(xp), inline=True)
        embed.add_field(
            name=t("leveling.field_next_level"),
            value=t("leveling.next_level_value", remaining=remaining, percent=int(prog * 100)),
            inline=True,
        )
        return embed

    def leaderboard_embed(self, guild_id: int | str, name_resolver=None):
        settings = self._cached_settings(guild_id, time.monotonic())
        rows = self._store.leaderboard(guild_id, self._top_n(settings))
        if not rows:
            desc = t("leveling.leaderboard_empty")
        else:
            lines = []
            for rank, (uid, xp) in enumerate(rows, start=1):
                name = name_resolver(uid) if name_resolver is not None else f"<@{uid}>"
                lines.append(t("leveling.leaderboard_row", rank=rank, name=name, level=level(xp), xp=xp))
            desc = "\n".join(lines)
        return discord.Embed(title=t("leveling.leaderboard_title"), description=desc, color=_EMBED_COLOR)
