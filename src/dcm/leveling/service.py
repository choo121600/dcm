"""LevelingService — 핫패스 XP 적립 + 표시(embed) + (G003/G004에서 게이팅·역할).

record_message 는 on_message 핫패스에서 호출된다. 핫패스 DB READ 0 (steady state) 를 위해:
- 쿨다운은 인메모리 per-(guild,user) dict 1차 게이트(P1/F6).
- per-guild 설정(leveling_enabled/cooldown/top_n)은 read-through TTL 캐시로 보관해, 캐시 히트
  시 guild_settings SQLite read 가 발생하지 않는다(캐시 미스 시에만 TTL 당 1회 read).
어떤 실패도 침묵 degrade(멘션 경로·봇 동작 영향 0). 쿨다운 dict 는 주기적으로 stale 엔트리를
sweep 해 무한 증가를 막는다.
"""
from __future__ import annotations

import logging
import time

import discord

from .scoring import level, progress, utc_day, xp_award, xp_to_next
from .store import LevelingStore

log = logging.getLogger(__name__)

DEFAULT_COOLDOWN_SECONDS = 60.0
DEFAULT_TOP_N = 10
SETTINGS_CACHE_TTL = 60.0  # per-guild 설정 캐시 수명(초, monotonic) — 변경은 이 시간 내 전파
_EVICT_THRESHOLD = 5_000  # _last_award 가 이 크기를 넘으면 stale 엔트리 sweep
_EVICT_MAX_AGE = 3_600.0  # 이보다 오래된 쿨다운 엔트리는 안전 제거(어떤 쿨다운도 지났음)
_EMBED_COLOR = 0x5865F2  # Discord blurple
# 신뢰 게이팅 쿼터 티어(코드 기본값): (최소 레벨, 일일 한도). 레벨에서 파생.
WEB_QUOTA_TIERS = ((0, 20), (5, 50), (10, 100))
LLM_QUOTA_TIERS = ((0, 100), (5, 300), (10, 600))
# 명시적 '위험' 권한(거부 사유 dangerous:* 라벨). 아래 SAFE allow-list 와 함께 fail-closed 동작.
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

# fail-closed allow-list: 무인 자동부여 역할은 아래 '안전' 권한 외 어떤 권한이라도 가지면 거부한다.
# (단순 deny-list 가 아님 — 알 수 없는/미래 Discord 권한도 SAFE 에 없으면 자동 거부.)
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
    """검사할 권한 이름 = 명시적 위험 + (가능하면) Discord 전체 플래그(fail-closed 보장)."""
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
        # 인메모리 쿨다운: (guild_id, user_id) -> monotonic 마지막 적립 시각.
        self._last_award: dict[tuple[str, str], float] = {}
        # per-guild 설정 read-through 캐시: guild_id -> (fetched_monotonic, settings|None).
        self._settings_cache: dict[str, tuple[float, object]] = {}

    # --- per-guild 설정 (TTL 캐시 + 실패 시 기본값 degrade) ---

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

    def _maybe_evict(self, mono: float) -> None:
        if len(self._last_award) < _EVICT_THRESHOLD:
            return
        cutoff = mono - _EVICT_MAX_AGE
        self._last_award = {k: v for k, v in self._last_award.items() if v >= cutoff}

    # --- 핫패스 ---

    def record_message(
        self,
        guild_id: int | str,
        user_id: int | str,
        text: str,
        *,
        now: float | None = None,
        monotonic_time: float | None = None,
    ) -> bool:
        """비봇 메시지에 질-가중 XP 적립. 인메모리 쿨다운 1차 게이트 + 설정 TTL 캐시(핫패스 DB READ 0).

        반환: XP 가 적립되면 True, (비활성/쿨다운/무가치) skip 이면 False. 예외는 침묵 degrade.
        """
        try:
            mono = monotonic_time if monotonic_time is not None else time.monotonic()
            settings = self._cached_settings(guild_id, mono)
            if not self._enabled(settings):
                return False
            key = (str(guild_id), str(user_id))
            last = self._last_award.get(key)
            if last is not None and mono - last < self._cooldown(settings):
                return False
            xp = xp_award(text)
            if xp <= 0:
                return False  # 빈/무가치 메시지는 쿨다운 소비 없이 skip
            self._last_award[key] = mono
            self._maybe_evict(mono)
            wall = now if now is not None else time.time()
            self._store.add_xp(guild_id, user_id, xp, now=wall)
            return True
        except Exception:  # noqa: BLE001
            log.exception("leveling record_message failed (silent degrade)")
            return False

    # --- 신뢰 게이팅 (G003/G004): 레벨별 일일 한도 ---

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
        """레벨 파생 일일 한도 대비 사용량 확인. 반환 (allowed, remaining).

        allowed=True 면 한도 내. 예외/조회 실패는 fail-open(allow)으로 degrade해 일시 오류가
        정상 기능을 막지 않게 한다(web 은 별도 쿨다운/비용 가드 존재).
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
        """성공한 web/llm 사용 1건을 일일 사용량에 반영(실패 침묵)."""
        try:
            d = day or utc_day(time.time())
            self._store.incr_daily_usage(guild_id, user_id, d, kind)
        except Exception:  # noqa: BLE001
            log.exception("leveling record_usage failed (silent)")

    # --- 레벨→역할 보상 (G004): allow-list 무인 부여 ---

    @staticmethod
    def _role_grant_ok(role, guild, bot_top_position) -> tuple[bool, str]:
        """무인 자동부여 안전 검사(fail-closed allow-list): 위계 통과 + SAFE 외 권한 0 인 장식 역할만 허용."""
        if getattr(role, "managed", False):
            return (False, "managed")
        default_role = getattr(guild, "default_role", None)
        if default_role is not None and role == default_role:
            return (False, "everyone")
        if bot_top_position is None:
            return (False, "no-bot-top-role")
        if getattr(role, "position", 0) >= bot_top_position:
            return (False, "hierarchy")  # 봇 최상위 역할 미만(strict)만
        perms = getattr(role, "permissions", None)
        if perms is not None:
            for name in _all_permission_names():
                if name in SAFE_PERMISSION_NAMES or not getattr(perms, name, False):
                    continue
                label = "dangerous" if name in DANGEROUS_PERMISSION_NAMES else "unsafe-perm"
                return (False, f"{label}:{name}")
        return (True, "ok")

    async def reconcile_roles(self, member) -> None:
        """멤버 레벨에 해당하는 매핑 역할을 allow-list 가드로 무인 부여(멱등·실패 침묵)."""
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
            for reward_level, role_id in rewards:
                if lvl < reward_level:
                    continue
                role = guild.get_role(role_id) if hasattr(guild, "get_role") else None
                if role is None:
                    continue  # 삭제된 역할 skip
                if role in member_roles:
                    continue  # 멱등: 이미 보유
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
                except Exception:  # noqa: BLE001 (discord.Forbidden/HTTPException 등)
                    log.exception("leveling add_roles failed (silent degrade)")
        except Exception:  # noqa: BLE001
            log.exception("reconcile_roles failed (silent degrade)")

    # --- 역할 보상 설정 (admin) ---

    def validate_reward_role(self, role, guild) -> tuple[bool, str]:
        """설정 시점 allow-list 사전검증(부여 시점과 동일 가드)."""
        bot_member = getattr(guild, "me", None)
        bot_top = getattr(getattr(bot_member, "top_role", None), "position", None)
        return self._role_grant_ok(role, guild, bot_top)

    def set_role_reward(self, guild_id: int | str, level: int, role_id: int) -> None:
        self._store.set_role_reward(guild_id, level, role_id)

    def remove_role_reward(self, guild_id: int | str, level: int) -> None:
        self._store.remove_role_reward(guild_id, level)

    def list_role_rewards(self, guild_id: int | str) -> list[tuple[int, int]]:
        return self._store.get_role_rewards(guild_id)

    # --- 표시 (embed) ---

    def rank_embed(self, guild_id: int | str, user_id: int | str, display_name: str):
        xp, _ = self._store.get_record(guild_id, user_id)
        lvl = level(xp)
        prog = progress(xp)
        remaining = xp_to_next(xp)
        embed = discord.Embed(title=f"{display_name} 님의 활동 레벨", color=_EMBED_COLOR)
        embed.add_field(name="레벨", value=str(lvl), inline=True)
        embed.add_field(name="총 XP", value=str(xp), inline=True)
        embed.add_field(
            name="다음 레벨까지", value=f"{remaining} XP ({int(prog * 100)}%)", inline=True
        )
        return embed

    def leaderboard_embed(self, guild_id: int | str, name_resolver=None):
        settings = self._cached_settings(guild_id, time.monotonic())
        rows = self._store.leaderboard(guild_id, self._top_n(settings))
        if not rows:
            desc = "아직 활동 기록이 없어요. 채팅하면 XP가 쌓여요!"
        else:
            lines = []
            for rank, (uid, xp) in enumerate(rows, start=1):
                name = name_resolver(uid) if name_resolver is not None else f"<@{uid}>"
                lines.append(f"**{rank}.** {name} — Lv {level(xp)} ({xp} XP)")
            desc = "\n".join(lines)
        return discord.Embed(title="🏆 활동 리더보드", description=desc, color=_EMBED_COLOR)
