from __future__ import annotations

import asyncio
import logging
import time

import discord

from ..i18n import t
from ..service import guild_admin as policy
from .base import (
    BufferedMessage,
    ChatPlatform,
    GuildAdmin,
    IncomingMessage,
    MentionHandler,
    is_admin,
)

log = logging.getLogger(__name__)

DISCORD_MAX = 2000

# Korean vocative particles appended when addressing the bot by name (썩스가재야/썩스가재님/썩스가재씨).
_VOCATIVE = "야아님씨"
# Boundary characters that may follow the name when called by name alone ("썩스가재 안녕", "썩스가재!", "썩스가재?").
_NAME_BOUNDARY = " \t\n\r!?.,~…:;"

# Server-template attachment handling (NL path): allowed extensions / max size / setup-intent keywords.
_TEMPLATE_EXTS = (".yaml", ".yml", ".json")
_TEMPLATE_MAX_BYTES = 256 * 1024
_SETUP_INTENT_WORDS = (
    "세팅", "셋업", "set up", "setup", "적용", "템플릿", "template",
    "구성", "세트업", "블루프린트", "blueprint", "만들어",
)

# Server-structure → YAML export (NL path) intent detection: only when a format word and an action word are both present.
_EXPORT_FORMAT_WORDS = ("yaml", "json", "템플릿", "블루프린트", "blueprint")
_EXPORT_ACTION_WORDS = ("뽑", "추출", "내보내", "백업", "스냅샷", "snapshot", "export", "dump", "확인", "보여", "구조")

# Anti-monopoly nudge (anti-fatigue): when a single user keeps up a back-to-back 1:1 conversation
# with the bot in one channel (a streak), a nudge is given at that point. Style is one of
# divider (cut line, default) / thread (move to a thread) / off.
_NUDGE_STREAK = 5  # when consecutive bot replies to the same user reach this value, the nudge fires
_NUDGE_COOLDOWN = 600.0  # cooldown (seconds) before the nudge can fire again in that channel
_NUDGE_STYLES = frozenset({"divider", "thread", "off"})
# divider cut line: Discord does not render '---' as a divider, so a box-drawing dotted line + scissors (✂) produce an actual cut-line shape.
# The copy is externalized to the i18n catalog (adapter.cutline); kept as a module constant for back-compat/import.
_CUTLINE = t("adapter.cutline")
_THREAD_AUTO_ARCHIVE_MIN = 1440  # (thread style) thread auto-archive (minutes): 24h
# Discord channel types treated as threads — if inside one, it's already a thread, so no nudge.
_THREAD_CHANNEL_TYPES = frozenset(
    t
    for t in (
        getattr(discord.ChannelType, "public_thread", None),
        getattr(discord.ChannelType, "private_thread", None),
        getattr(discord.ChannelType, "news_thread", None),
    )
    if t is not None
)


def split_for_discord(text: str, limit: int = DISCORD_MAX) -> list[str]:
    """Split text into chunks that fit Discord's per-message limit, preferring line breaks."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
        current += line
    if current:
        chunks.append(current)
    return chunks or [""]


class PycordAdapter(ChatPlatform, GuildAdmin):
    """Pycord implementation of ChatPlatform (ARCHITECTURE.md §3). The only module that imports discord."""

    def __init__(
        self,
        token: str,
        bot_name: str,
        buffer_size: int = 12,
        cooldown_seconds: float = 3.0,
        guild_id: int = 0,
        admin_role_id: int = 0,
        onboarding_policy=None,
        guild_settings=None,
        announcements=None,
        events=None,
        llm=None,
        nudge_style: str = "divider",
    ) -> None:
        self._token = token
        self._bot_name = bot_name
        self._buffer_size = buffer_size
        self._cooldown = cooldown_seconds
        self._guild_id = guild_id  # guild for guild-scoped slash registration (ralplan S1/S2)
        self._admin_role_id = admin_role_id  # designated ADMIN_ROLE for the InvokerCheck (S2)
        self._last_seen: dict[str, float] = {}  # author_id → monotonic ts (cooldown, §14.4)
        # Anti-monopoly nudge (anti-fatigue): per-channel (last-replied author_id, consecutive streak) + re-fire cooldown.
        self._nudge_style = nudge_style if nudge_style in _NUDGE_STYLES else "divider"
        self._chan_addresser: dict[str, str] = {}
        self._chan_streak: dict[str, int] = {}
        self._nudge_cooldown: dict[str, float] = {}
        self._handler: MentionHandler | None = None
        self._service = None  # GuildAdmin policy service, wired in register_admin_commands (S2)
        self._pending = policy.PendingConfirmations()  # adapter-local confirm-token carry (S2)
        self._admin_commands: list[str] = []  # registry for the by-construction InvokerCheck test
        self._rl = policy.RateLimiter()  # additive burst-smoothing over pycord 429 handling (S6)
        self._onboarding = onboarding_policy  # OnboardingPolicy instance (S6 onboarding)
        self._settings = guild_settings  # per-guild settings/admin role (multi-guild v2); falls back to env seed values if None
        self._public_commands: list[str] = []  # registry of unguarded member-facing public commands (leveling display)
        self._leveling = None  # LevelingService, injected in register_leveling_commands
        self._announcements = announcements  # scheduled-announcement store (AnnouncementStore); disabled if None
        self._events = events  # event countdown-announcement store (EventStore); disabled if None
        self._llm = llm  # LLMClient (optional). Used only for polishing announcement/event copy; polishing disabled if None
        self._announce_task = None

        intents = discord.Intents.default()
        # Privileged — must also be enabled in the Developer Portal (ARCHITECTURE.md §14.2).
        intents.message_content = True
        intents.members = True  # required to fire on_member_join (S6 onboarding; must also be enabled in the Developer Portal)
        # discord.Bot (subclass of Client) so application/slash commands register & auto-sync.
        # Bot preserves the inherited mention path (on_message/get_channel/start/user) — ralplan S1.
        self._client = discord.Bot(intents=intents)
        self._client.event(self.on_ready)
        self._client.event(self.on_message)
        self._client.event(self.on_member_join)
        # Handle the study-join button (studyjoin:<role_id>) — using add_listener so it coexists with the default slash handling (on_interaction).
        self._client.add_listener(self._on_component_interaction, "on_interaction")

    def on_mention(self, handler: MentionHandler) -> None:
        self._handler = handler

    @property
    def pending(self):
        """Adapter-local pending high-risk confirmations (shared with the policy service, S2)."""
        return self._pending

    async def on_ready(self) -> None:
        log.info("%s online as %s", self._bot_name, self._client.user)
        # For cutover verification: lets the operator confirm from the logs whether the privileged intents are enabled (ARCHITECTURE.md §14.2, S7).
        intents = self._client.intents
        log.info("privileged intent message_content=%s", intents.message_content)
        log.info("privileged intent members=%s", intents.members)
        if (self._announcements is not None or self._events is not None) and self._announce_task is None:
            self._announce_task = asyncio.create_task(self._announce_loop())
            log.info("scheduled-announcement loop started")

    async def _announce_loop(self) -> None:
        """Check scheduled announcements every minute and fire them (silently degrade on error). Started once from on_ready."""
        from ..service.announcements import (
            due_event_leads,
            is_due,
            minute_key,
            render_event_message,
        )

        while True:
            try:
                now = time.time()
                if self._announcements is not None:
                    for ann in self._announcements.list_enabled():
                        if not is_due(ann, now):
                            continue
                        try:
                            channel = self._client.get_channel(int(ann.channel_id))
                            if channel is not None:
                                await self._send_to(channel, ann.message)
                            self._announcements.mark_fired(ann.id, minute_key(now))
                            if ann.run_at is not None:  # one-shot → disable after firing
                                self._announcements.set_enabled(ann.id, ann.guild_id, False)
                        except Exception:  # noqa: BLE001 - swallow individual announcement failures, keep the loop alive
                            log.exception("announcement %s fire failed", ann.id)
                if self._events is not None:
                    for evt in self._events.list_enabled():
                        try:
                            leads = due_event_leads(evt, now)
                            if leads:
                                # Label is based on the actual days remaining (accurate D-N even when registered late / backlogged).
                                days = max(0, round((evt.event_at - now) / 86400))
                                channel = self._client.get_channel(int(evt.channel_id))
                                if channel is not None:
                                    await self._send_to(channel, render_event_message(evt, days))
                                for lead in leads:  # mark both the fired lead and any overdue past ones
                                    self._events.mark_lead_fired(evt.id, lead)
                            if now > evt.event_at + 3600:  # event over → disable
                                self._events.set_enabled(evt.id, evt.guild_id, False)
                        except Exception:  # noqa: BLE001 - swallow individual event failures, keep the loop alive
                            log.exception("event %s fire failed", evt.id)
            except Exception:  # noqa: BLE001
                log.exception("announcement loop tick failed")
            await asyncio.sleep(60)

    async def _on_component_interaction(self, interaction) -> None:
        """Handle clicks on the study-join button (studyjoin:<role_id>). Ignore other interactions (slash goes through the bot's default flow)."""
        try:
            if interaction.type != discord.InteractionType.component:
                return
            data = interaction.data if isinstance(interaction.data, dict) else {}
            cid = data.get("custom_id", "")
            if not cid.startswith("studyjoin:"):
                return
            try:
                role_id = int(cid.split(":", 1)[1])
            except ValueError:
                return
            await self._toggle_self_role(interaction, role_id)
        except Exception:  # noqa: BLE001 - swallow button-handling failures (log only)
            log.exception("study-join interaction failed")

    async def _toggle_self_role(self, interaction, role_id: int) -> None:
        """Self-toggle a study role. Only permission-less (view-only) roles are allowed — self-granting roles that carry permissions is blocked."""
        guild = interaction.guild
        role = guild.get_role(role_id) if guild else None
        member = getattr(interaction, "user", None)
        if guild is None or member is None:
            await interaction.response.send_message(t("adapter.guild_only"), ephemeral=True)
            return
        if role is None:
            await interaction.response.send_message(t("adapter.study_role_gone"), ephemeral=True)
            return
        # Roles granting permissions beyond @everyone cannot be self-granted (blocks privilege escalation). Study roles are
        # equal to @everyone or have no permissions → allowed. (If permissions are unspecified at role creation, the @everyone value is copied.)
        everyone_perms = guild.default_role.permissions.value if getattr(guild, "default_role", None) else 0
        if (role.permissions.value & ~everyone_perms) != 0:
            await interaction.response.send_message(
                t("adapter.role_has_perms"), ephemeral=True
            )
            return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason=t("adapter.reason_self_leave"))
                await interaction.response.send_message(t("adapter.study_left", role_name=role.name), ephemeral=True)
            else:
                await member.add_roles(role, reason=t("adapter.reason_self_join"))
                await interaction.response.send_message(
                    t("adapter.study_joined", role_name=role.name), ephemeral=True
                )
        except discord.Forbidden:
            await interaction.response.send_message(t("adapter.role_change_forbidden"), ephemeral=True)

    async def on_message(self, message: discord.Message) -> None:
        # Ignore self and other bots.
        if message.author.bot or message.author == self._client.user:
            return
        # Ignore DMs (no guild) entirely — under multi-guild isolation, a path without guild_id is fail-closed (P2 / G5).
        if message.guild is None:
            return

        # Activity leveling: award XP on every non-bot message (in-memory cooldown + settings TTL cache → 0 steady-state DB reads, failures swallowed).
        # Placed before _is_addressed so ordinary (non-mention) messages are counted too (P1).
        if self._leveling is not None:
            awarded = self._leveling.record_message(
                message.guild.id,
                message.author.id,
                message.content or "",
                mention_count=len(message.mentions) + len(getattr(message, "role_mentions", None) or []),
                is_admin=self._has_manage_guild(message.author),
                on_penalty=lambda: asyncio.create_task(self._safe_reconcile(message.author)),
            )
            if awarded:
                # Only on an award (cooldown passed), attempt the unattended level→role grant (idempotent, allow-list guarded, G004).
                asyncio.create_task(self._safe_reconcile(message.author))
        # Address detection: @mention / reply to a bot message / name call in the message ("썩스가재야 …", "안녕 썩스가재야").
        # Name calls allow natural addressing while curbing spam (ARCHITECTURE.md §14.4; persona.md examples).
        if not self._is_addressed(message):
            return

        # Per-user cooldown to curb spam / cost (ARCHITECTURE.md §14.4).
        author_id = str(message.author.id)
        now = time.monotonic()
        last = self._last_seen.get(author_id)
        if last is not None and now - last < self._cooldown:
            return
        self._last_seen[author_id] = now

        # Server-template attachment (.yaml/.json) + setup intent → branch to the template-apply path (admins/owner only).
        attachment = self._template_attachment(message)
        if attachment is not None and self._wants_setup(self._strip_mentions(message)):
            await self._handle_template_attachment(message, attachment)
            return

        # Without an attachment, "export the server structure as yaml" → export the current structure (admins/owner only).
        if attachment is None and self._wants_export(self._strip_mentions(message)):
            await self._handle_export(message)
            return

        if self._handler is None:
            return

        incoming = IncomingMessage(
            channel_id=str(message.channel.id),
            author_id=str(message.author.id),
            author_name=message.author.display_name,
            content=self._strip_mentions(message),
            role_ids=self._role_ids(message.author),
            is_owner=self._is_owner(message),
            guild_id=int(message.guild.id),
            admin_role_id=self._guild_admin_role(message.guild.id),
            has_manage_guild=self._has_manage_guild(message.author),
        )
        buffer = await self._history(message.channel, self._buffer_size)

        try:
            async with message.channel.typing():
                reply = await self._handler(incoming, buffer)
        except Exception:
            log.exception("mention handler failed")
            reply = None

        if reply:
            await self._deliver_reply(message, reply)

    async def on_member_join(self, member) -> None:
        """New-member join event handler (S6 onboarding).

        After OnboardingPolicy.decide() determines the behavior:
        - if welcome_channel_id is set, send welcome_text to that channel.
        - if default_role_id is set, grant that role to the member.
        Exceptions are recorded via log.exception but silently degrade so the bot does not die.
        """
        if self._onboarding is None:
            return
        if getattr(member, "bot", False):
            return  # do not onboard bots that join (S6)

        try:
            gid = getattr(getattr(member, "guild", None), "id", None)
            decision = self._onboarding.decide(member.display_name, guild_id=gid)
        except Exception:
            log.exception("on_member_join: onboarding decision failed (silent degrade)")
            return

        # Separate the welcome message and role grant into independent try blocks — one failing must not block the other.
        if decision.welcome_channel_id is not None and decision.welcome_text is not None:
            try:
                channel = self._client.get_channel(decision.welcome_channel_id)
                if channel is not None:
                    await channel.send(decision.welcome_text)
                else:
                    log.warning(
                        "on_member_join: welcome channel %d not found",
                        decision.welcome_channel_id,
                    )
            except Exception:
                log.exception("on_member_join: welcome message send failed (silent degrade)")

        if decision.default_role_id is not None:
            try:
                guild = getattr(member, "guild", None)
                if guild is not None:
                    role = guild.get_role(decision.default_role_id)
                    if role is not None:
                        await member.add_roles(role, reason=t("adapter.reason_onboarding_role"))
                    else:
                        log.warning(
                            "on_member_join: default_role %d not found",
                            decision.default_role_id,
                        )
            except Exception:
                log.exception("on_member_join: automatic role grant failed (silent degrade)")

    def _is_addressed(self, message: discord.Message) -> bool:
        """Decide whether the message is addressed to the bot: @mention / reply to a bot message / name call."""
        # 1) Direct @mention (@everyone/@here do not appear in message.mentions).
        if self._client.user in message.mentions:
            return True
        # 2) A reply to a message the bot sent.
        ref = getattr(message, "reference", None)
        resolved = getattr(ref, "resolved", None) if ref is not None else None
        if resolved is not None and getattr(resolved, "author", None) == self._client.user:
            return True
        # 3) Called by name.
        return self._name_called(message.content)

    def _name_called(self, content: str) -> bool:
        """Return True if the message addresses the bot by name.

        Matches "썩스가재"/"썩스가재야"/"썩스가재님 …"/"썩스가재 안녕"/"안녕 썩스가재야",
        but excludes third-person references like "썩스가재가/썩스가재는" (spam guard).
        """
        name = (self._bot_name or "").strip()
        text = (content or "").strip()
        if not name or not text:
            return False
        # (a) Leading call: the name is followed by end-of-string / a vocative particle / a boundary character.
        if text.startswith(name):
            rest = text[len(name):]
            if rest == "" or rest[0] in _VOCATIVE or rest[0] in _NAME_BOUNDARY:
                return True
        # (b) A standalone "name+vocative particle" token anywhere in the sentence ("안녕 썩스가재야").
        idx = text.find(name)
        while idx != -1:
            before_ok = idx == 0 or text[idx - 1] in _NAME_BOUNDARY
            after = text[idx + len(name):]
            if before_ok and after and after[0] in _VOCATIVE:
                return True
            idx = text.find(name, idx + 1)
        return False

    def _strip_mentions(self, message: discord.Message) -> str:
        content = message.content
        for user in message.mentions:
            content = content.replace(f"<@{user.id}>", "").replace(f"<@!{user.id}>", "")
        return content.strip()

    async def _history(self, channel, n: int) -> list[BufferedMessage]:
        msgs: list[BufferedMessage] = []
        async for m in channel.history(limit=n):
            msgs.append(
                BufferedMessage(
                    author_name=m.author.display_name,
                    content=m.clean_content,
                    is_bot=m.author.bot,
                )
            )
        msgs.reverse()  # oldest first
        return msgs

    async def _send_to(self, channel, text: str) -> None:
        for chunk in split_for_discord(text):
            await channel.send(chunk)

    async def _deliver_reply(self, message, reply) -> None:
        """Send the reply, but if the anti-monopoly nudge fires, handle it per the configured style (anti-fatigue).

        - thread: create a public thread on that message and send the reply there (keeps the main channel tidy).
        - divider: leave the reply in the channel and append a single cut line (✂) as a 'cut here' signal.
        - off/not fired: just send the reply to the original channel as usual.
        """
        if not self._nudge_due(message):
            await self._send_to(message.channel, reply)
            return
        if self._nudge_style == "thread":
            thread = await self._open_thread(message)
            await self._send_to(thread or message.channel, reply)
        else:  # "divider": keep the channel + a single cut line
            await self._send_to(message.channel, reply)
            await self._send_to(message.channel, t("adapter.cutline"))

    def _nudge_due(self, message) -> bool:
        """Decide whether this reply should fire the anti-monopoly nudge, updating streak/cooldown state.

        Returns True at _NUDGE_STREAK when the same user calls the bot consecutively (a streak) in one
        channel. False if already inside a thread, within the cooldown, or style=off. When returning
        True, it resets the streak and starts the cooldown.
        """
        if self._nudge_style not in ("thread", "divider"):
            return False
        channel = getattr(message, "channel", None)
        if channel is None or getattr(channel, "type", None) in _THREAD_CHANNEL_TYPES:
            return False  # no channel / already a thread → no nudge
        cid = str(getattr(channel, "id", ""))
        author_id = str(getattr(getattr(message, "author", None), "id", ""))
        # Update the consecutive streak: +1 for the same user, reset to 1 for a different user.
        if self._chan_addresser.get(cid) == author_id:
            streak = self._chan_streak.get(cid, 0) + 1
        else:
            streak = 1
            self._chan_addresser[cid] = author_id
        self._chan_streak[cid] = streak
        if streak < _NUDGE_STREAK:
            return False
        now = time.monotonic()
        until = self._nudge_cooldown.get(cid)
        if until is not None and now < until:
            return False  # nudged recently in this channel → do not re-fire for a while
        self._chan_streak[cid] = 0  # fired → reset the streak + start the cooldown (prevents nudge spam)
        self._nudge_cooldown[cid] = now + _NUDGE_COOLDOWN
        return True

    async def _open_thread(self, message):
        """(thread style) Create and return a public thread on the message. Returns None on failure/unavailable (keeps the original channel)."""
        if not hasattr(message, "create_thread"):
            return None
        try:
            return await message.create_thread(
                name=self._thread_name(getattr(message, "author", None)),
                auto_archive_duration=_THREAD_AUTO_ARCHIVE_MIN,
            )
        except Exception:
            log.exception("thread creation failed (silent degrade)")
            return None

    @staticmethod
    def _thread_name(author) -> str:
        """Thread name (safely truncated to Discord's 100-character limit)."""
        name = getattr(author, "display_name", None) or t("adapter.thread_name_default")
        return t("adapter.thread_name", name=name)[:100]

    def _template_attachment(self, message):
        """Return the message's first server-template attachment (.yaml/.yml/.json), or None if there is none."""
        for att in getattr(message, "attachments", None) or []:
            fn = (getattr(att, "filename", "") or "").lower()
            if fn.endswith(_TEMPLATE_EXTS):
                return att
        return None

    @staticmethod
    def _wants_setup(text: str) -> bool:
        """True if a setup/apply-intent keyword is present (decides whether to treat the attachment as a template)."""
        low = (text or "").lower()
        return any(w in low for w in _SETUP_INTENT_WORDS)

    async def _handle_template_attachment(self, message, attachment) -> None:
        """'썩스가재야 + template attachment + setup' NL path: admins/owner only, preview → apply via a confirm button.

        The file content is passed only to the apply_template parser (data only) — it never enters memory/persona.
        The confirm button on the public message re-checks the clicker's permission.
        """
        author = message.author
        if not self._is_admin(message):
            await self._send_to(
                message.channel, t("adapter.template_admin_only")
            )
            return
        if self._service is None:
            await self._send_to(message.channel, t("adapter.admin_disabled"))
            return
        if int(getattr(attachment, "size", 0) or 0) > _TEMPLATE_MAX_BYTES:
            await self._send_to(message.channel, t("adapter.template_too_big"))
            return
        try:
            raw = (await attachment.read()).decode("utf-8")
        except Exception:
            await self._send_to(
                message.channel, t("adapter.template_not_utf8")
            )
            return
        gid = int(getattr(message.guild, "id", 0) or self._guild_id)
        an = getattr(author, "display_name", "?")
        aid = int(getattr(author, "id", 0))

        def factory(token):
            return self._service.apply_template(
                guild_id=gid, actor_name=an, actor_id=aid, template_text=raw, confirm_token=token
            )

        try:
            res = await factory(None)  # dry run: parse + preview (no creation)
        except Exception:
            log.exception("template dry-run failed")
            await self._send_to(message.channel, t("adapter.template_process_error"))
            return
        if not res.needs_confirmation:
            await self._send_to(message.channel, res.detail)  # parse error or immediate result
            return

        def authorized(interaction) -> bool:
            return self._is_admin(interaction)

        view = _ConfirmView(factory, res.confirmation_token, authorized=authorized)
        await message.channel.send(
            t("adapter.template_apply_prompt", detail=res.detail),
            view=view,
        )

    @staticmethod
    def _wants_export(text: str) -> bool:
        """True for an export intent like 'server structure to yaml' (a format word and an action word both present)."""
        low = (text or "").lower()
        return any(w in low for w in _EXPORT_FORMAT_WORDS) and any(
            w in low for w in _EXPORT_ACTION_WORDS
        )

    @staticmethod
    def _overwrite_info(channel, default_role, role_set):
        """Extract (private, names of roles allowed to view) from a channel's permission overwrites."""
        private = False
        visible: list[str] = []
        for target, ow in (getattr(channel, "overwrites", None) or {}).items():
            view = getattr(ow, "view_channel", None)
            if target == default_role:
                if view is False:
                    private = True
            elif target in role_set and view is True:
                visible.append(getattr(target, "name", ""))
        return private, visible

    def _snapshot_template(self, guild):
        """Extract the current guild structure into a ServerTemplate (roles/categories/channels + private detection)."""
        from ..service.template import (
            SUPPORTED_PERM_MASK,
            ServerTemplate,
            TemplateCategory,
            TemplateChannel,
            TemplateRole,
            bits_to_names,
        )

        default_role = getattr(guild, "default_role", None)
        role_objs = list(getattr(guild, "roles", []))
        role_set = role_objs  # membership tested via list (==) — safe for both discord.Role and test doubles (no hashing required)
        roles = []
        for r in reversed(role_objs):  # highest roles first
            if r == default_role or getattr(r, "managed", False):
                continue
            bits = int(getattr(getattr(r, "permissions", None), "value", 0))
            roles.append(
                TemplateRole(
                    name=r.name,
                    permission_bits=bits & SUPPORTED_PERM_MASK,
                    permission_names=tuple(bits_to_names(bits)),
                )
            )
        cats = []
        for cat in getattr(guild, "categories", []):
            private, visible = self._overwrite_info(cat, default_role, role_set)
            chans = []
            for ch in getattr(cat, "channels", []):
                t = getattr(ch, "type", None)
                if t == discord.ChannelType.voice:
                    chans.append(TemplateChannel(name=ch.name, kind="voice"))
                elif t == discord.ChannelType.text:
                    chans.append(TemplateChannel(name=ch.name, kind="text"))
            cats.append(
                TemplateCategory(
                    name=cat.name, channels=tuple(chans), private=private, visible_to=tuple(visible)
                )
            )
        return ServerTemplate(roles=tuple(roles), categories=tuple(cats))

    def _export_server_yaml(self, guild) -> str:
        """Guild → YAML text (including a header comment). Top-level channels without a category are noted in a comment."""
        from ..service.template import to_yaml

        body = to_yaml(self._snapshot_template(guild))
        orphans = [
            c.name
            for c in getattr(guild, "channels", [])
            if getattr(c, "category", None) is None
            and getattr(c, "type", None) in (discord.ChannelType.text, discord.ChannelType.voice)
        ]
        header = t("adapter.export_header")
        if orphans:
            header += t("adapter.export_orphan_note", orphans=", ".join(orphans))
        return header + body

    async def _handle_export(self, message) -> None:
        """'썩스가재야 export the server structure as yaml' NL path: admins/owner only, reply with the current structure as a YAML file."""
        if not self._is_admin(message):
            await self._send_to(
                message.channel, t("adapter.export_admin_only")
            )
            return
        guild = getattr(message, "guild", None)
        if guild is None:
            await self._send_to(message.channel, t("adapter.guild_only"))
            return
        try:
            text = self._export_server_yaml(guild)
        except Exception:
            log.exception("export failed")
            await self._send_to(message.channel, t("adapter.export_error"))
            return
        import io

        fp = io.BytesIO(text.encode("utf-8"))
        await message.channel.send(
            t("adapter.export_file_msg"),
            file=discord.File(fp, filename="server-template.yaml"),
        )

    # --- InvokerCheck + by-construction admin-command registration (ralplan S2) ---

    @staticmethod
    def _role_ids(member) -> frozenset[int]:
        """Resolve a Discord member's role ids to plain data (ralplan S2)."""
        return frozenset(int(r.id) for r in getattr(member, "roles", None) or [])

    @staticmethod
    def _has_manage_guild(member) -> bool:
        """Whether the caller holds Discord Manage Guild or Administrator permission (adapter-local interpretation)."""
        perms = getattr(member, "guild_permissions", None)
        if perms is None:
            return False
        return bool(getattr(perms, "manage_guild", False) or getattr(perms, "administrator", False))

    def _guild_admin_role(self, guild_id) -> int:
        """This guild's configured admin-role id (0 if none → authz falls back to has_manage_guild). Multi-guild v2.
        If settings is not injected (single-guild compatibility), use the env seed admin_role_id."""
        if self._settings is None:
            return int(self._admin_role_id or 0)
        try:
            return int(self._settings.get(int(guild_id)).admin_role_id or 0)
        except Exception:
            return 0

    @staticmethod
    def _is_owner(obj) -> bool:
        """True if the caller is the guild owner. Works for both messages (on_message) and slash ctx.

        A Discord guild owner inherently holds every permission, so they are treated as an admin regardless of the designated role.
        Returns False when there is no guild (e.g. a DM).
        """
        guild = getattr(obj, "guild", None)
        if guild is None:
            return False
        owner_id = getattr(guild, "owner_id", None)
        author = getattr(obj, "author", None) or getattr(obj, "user", None)
        author_id = getattr(author, "id", None)
        return owner_id is not None and author_id is not None and int(owner_id) == int(author_id)

    def _is_admin(self, ctx) -> bool:
        """InvokerCheck (ralplan S2): the SOLE authz boundary. The caller must hold the
        configured ADMIN_ROLE (admin_role_id) — replaces the jiwoo Manage-Guild check.
        Authz is bound to Discord role membership in code, never inferred from message text;
        @default_permissions is only a UI hint and is intentionally NOT relied on."""
        member = getattr(ctx, "author", None) or getattr(ctx, "user", None)
        gid = self._ctx_guild(ctx)
        return is_admin(
            self._role_ids(member),
            self._guild_admin_role(gid),
            self._is_owner(ctx),
            self._has_manage_guild(member),
        )

    def admin_command(self, name: str, description: str, **kwargs):
        """Register a global admin slash command with the InvokerCheck injected by construction
        (ralplan S2 / multi-guild v2 P4). Registered globally (no guild_ids) so the command appears
        in every server the bot inhabits. The wrapped callback runs only after the InvokerCheck
        passes; non-admins get an ephemeral denial."""
        import functools
        import inspect

        def decorator(func):
            @functools.wraps(func)
            async def wrapped(ctx, *args, **kw):
                if not self._is_admin(ctx):
                    await ctx.respond(t("adapter.admin_only_command"), ephemeral=True)
                    return None
                return await func(ctx, *args, **kw)

            wrapped.__signature__ = inspect.signature(func)  # preserve options for pycord
            wrapped.__gjc_admin_guarded__ = True  # asserted by the by-construction test
            self._client.slash_command(
                name=name, description=description, **kwargs
            )(wrapped)
            self._admin_commands.append(name)
            return wrapped

        return decorator

    def public_command(self, name: str, description: str, **kwargs):
        """Register a member-facing (public) slash command with NO authz guard.

        Distinct from admin_command: registered globally so all members can invoke it,
        NOT appended to _admin_commands, and NOT marked __gjc_admin_guarded__. Used only
        for read-only leveling display (/rank, /leaderboard). authz invariant intact:
        every privileged path still routes through admin_command; public commands are
        intentionally unguarded reads of the caller's own / public leaderboard data."""

        def decorator(func):
            self._client.slash_command(name=name, description=description, **kwargs)(func)
            self._public_commands.append(name)
            return func

        return decorator

    def register_leveling_commands(self, leveling_service) -> None:
        """Wire member-facing leveling display commands (/rank, /leaderboard) — public,
        non-ephemeral, no admin guard so every member can view levels (spec f5/f9)."""
        self._leveling = leveling_service

        @self.public_command(name="rank", description=t("adapter.cmd_rank_desc"))
        async def rank(ctx):
            gid = self._ctx_guild(ctx)
            user = getattr(ctx, "author", None) or getattr(ctx, "user", None)
            display = getattr(user, "display_name", None) or str(getattr(user, "id", "?"))
            embed = leveling_service.rank_embed(gid, getattr(user, "id", 0), display)
            await ctx.respond(embed=embed)

        @self.public_command(
            name="leaderboard", description=t("adapter.cmd_leaderboard_desc")
        )
        async def leaderboard(ctx):
            gid = self._ctx_guild(ctx)
            guild = getattr(ctx, "guild", None)

            def resolve(uid):
                if guild is not None and hasattr(guild, "get_member"):
                    member = guild.get_member(int(uid))
                    if member is not None:
                        return getattr(member, "display_name", None) or f"<@{uid}>"
                return f"<@{uid}>"

            embed = leveling_service.leaderboard_embed(gid, resolve)
            await ctx.respond(embed=embed)

        @self.admin_command(
            name="set-level-role",
            description=t("adapter.cmd_set_level_role_desc"),
        )
        async def set_level_role(
            ctx,
            level: discord.Option(int, t("adapter.opt_threshold_level")),
            role: discord.Option(discord.Role, t("adapter.opt_grant_role")),
        ):
            gid = self._ctx_guild(ctx)
            guild = getattr(ctx, "guild", None)
            ok, reason = leveling_service.validate_reward_role(role, guild)
            if not ok:
                await ctx.respond(
                    t("adapter.reward_role_unsafe", reason=reason),
                    ephemeral=True,
                )
                return
            leveling_service.set_role_reward(gid, int(level), int(role.id))
            role_name = getattr(role, "name", role.id)
            await ctx.respond(
                t("adapter.level_role_set", level=int(level), role_name=role_name),
                ephemeral=True,
            )

        @self.admin_command(
            name="remove-level-role", description=t("adapter.cmd_remove_level_role_desc")
        )
        async def remove_level_role(ctx, level: discord.Option(int, t("adapter.opt_threshold_level"))):
            gid = self._ctx_guild(ctx)
            leveling_service.remove_role_reward(gid, int(level))
            await ctx.respond(t("adapter.level_role_removed", level=int(level)), ephemeral=True)

        @self.admin_command(
            name="list-level-roles", description=t("adapter.cmd_list_level_roles_desc")
        )
        async def list_level_roles(ctx):
            gid = self._ctx_guild(ctx)
            rewards = leveling_service.list_role_rewards(gid)
            if not rewards:
                await ctx.respond(t("adapter.no_level_roles"), ephemeral=True)
                return
            lines = "\n".join(t("adapter.level_role_line", lv=lv, rid=rid) for lv, rid in rewards)
            await ctx.respond(t("adapter.level_role_list", lines=lines), ephemeral=True)

    async def _safe_reconcile(self, member) -> None:
        """Safely run the unattended level→role grant in the background (silently degrade on error)."""
        if self._leveling is None:
            return
        try:
            await self._leveling.reconcile_roles(member)
        except Exception:  # noqa: BLE001
            log.exception("leveling reconcile task failed")

    def register_admin_commands(self, service) -> None:
        """Wire the guild-management slash-command surface (ralplan S2+). Every command is
        registered through admin_command so InvokerCheck is enforced by construction."""
        self._service = service

        @self.admin_command(
            name="whoami", description="Show your dcm server-management admin status."
        )
        async def whoami(ctx):
            # Only reached when InvokerCheck passed → the caller is a verified admin.
            await ctx.respond(
                t("adapter.whoami_ok"),
                ephemeral=True,
            )

        @self.admin_command(name="create-category", description="Create a category.")
        async def create_category(ctx, name: str):
            gid, (an, aid) = self._ctx_guild(ctx), self._actor(ctx)
            await self._run_op(
                ctx,
                lambda t: self._service.create_category(
                    guild_id=gid, actor_name=an, actor_id=aid, name=name
                ),
                confirm=False,
            )

        @self.admin_command(name="create-channel", description="Create a text or voice channel.")
        async def create_channel(ctx, name: str, kind: str = "text", category_id: str = ""):
            if kind not in ("text", "voice"):
                await ctx.respond(t("adapter.kind_invalid"), ephemeral=True)
                return
            gid, (an, aid) = self._ctx_guild(ctx), self._actor(ctx)
            cat = int(category_id) if category_id else None
            await self._run_op(
                ctx,
                lambda t: self._service.create_channel(
                    guild_id=gid, actor_name=an, actor_id=aid, name=name, kind=kind, category_id=cat
                ),
                confirm=False,
            )

        @self.admin_command(name="edit-channel", description="Rename or move a channel.")
        async def edit_channel(ctx, channel_id: str, new_name: str = "", category_id: str = ""):
            gid, (an, aid) = self._ctx_guild(ctx), self._actor(ctx)
            cat = int(category_id) if category_id else None
            await self._run_op(
                ctx,
                lambda t: self._service.edit_channel(
                    guild_id=gid,
                    actor_name=an,
                    actor_id=aid,
                    channel_id=int(channel_id),
                    name=(new_name or None),
                    category_id=cat,
                ),
                confirm=False,
            )

        @self.admin_command(
            name="delete-channel", description="Delete a channel (high-risk; needs confirm:true)."
        )
        async def delete_channel(ctx, channel_id: str, confirm: bool = False):
            gid, (an, aid) = self._ctx_guild(ctx), self._actor(ctx)
            await self._run_op(
                ctx,
                lambda t: self._service.delete_channel(
                    guild_id=gid, actor_name=an, actor_id=aid, channel_id=int(channel_id), confirm_token=t
                ),
                confirm=confirm,
            )

        @self.admin_command(
            name="create-role",
            description="Create a role (high-risk if it carries management perms; confirm).",
        )
        async def create_role(ctx, name: str, permissions: int = 0, confirm: bool = False):
            gid, (an, aid) = self._ctx_guild(ctx), self._actor(ctx)
            await self._run_op(
                ctx,
                lambda t: self._service.create_role(
                    guild_id=gid, actor_name=an, actor_id=aid, name=name, permissions=permissions, confirm_token=t
                ),
                confirm=confirm,
            )

        @self.admin_command(
            name="assign-role",
            description="Assign a role to a member (high-risk if the role carries management perms).",
        )
        async def assign_role(ctx, user_id: str, role_id: str, confirm: bool = False):
            gid, (an, aid) = self._ctx_guild(ctx), self._actor(ctx)
            await self._run_op(
                ctx,
                lambda t: self._service.assign_role(
                    guild_id=gid, actor_name=an, actor_id=aid, user_id=int(user_id), role_id=int(role_id), confirm_token=t
                ),
                confirm=confirm,
            )

        @self.admin_command(name="remove-role", description="Remove a role from a member.")
        async def remove_role(ctx, user_id: str, role_id: str):
            gid, (an, aid) = self._ctx_guild(ctx), self._actor(ctx)
            await self._run_op(
                ctx,
                lambda t: self._service.remove_role(
                    guild_id=gid, actor_name=an, actor_id=aid, user_id=int(user_id), role_id=int(role_id)
                ),
                confirm=False,
            )

        @self.admin_command(
            name="set-role-permissions",
            description="Set a role's permissions (high-risk if it includes management perms).",
        )
        async def set_role_permissions(ctx, role_id: str, permissions: int, confirm: bool = False):
            gid, (an, aid) = self._ctx_guild(ctx), self._actor(ctx)
            await self._run_op(
                ctx,
                lambda t: self._service.set_role_permissions(
                    guild_id=gid, actor_name=an, actor_id=aid, role_id=int(role_id), permissions=permissions, confirm_token=t
                ),
                confirm=confirm,
            )

        @self.admin_command(
            name="create-project",
            description="Create a project set: category + channels + private access role (high-risk).",
        )
        async def create_project(ctx, name: str, channels: str, access_role: str, confirm: bool = False):
            gid, (an, aid) = self._ctx_guild(ctx), self._actor(ctx)
            chans = [c.strip() for c in channels.split(",") if c.strip()]
            await self._run_op(
                ctx,
                lambda t: self._service.create_project(
                    guild_id=gid, actor_name=an, actor_id=aid, name=name, channels=chans, access_role_name=access_role, confirm_token=t
                ),
                confirm=confirm,
            )

        @self.admin_command(
            name="setup-server",
            description=t("adapter.cmd_setup_server_desc"),
        )
        async def setup_server(ctx, template: discord.Attachment, confirm: bool = False):
            try:
                raw = (await template.read()).decode("utf-8")
            except UnicodeDecodeError:
                await ctx.respond(
                    t("adapter.setup_not_utf8"),
                    ephemeral=True,
                )
                return
            gid, (an, aid) = self._ctx_guild(ctx), self._actor(ctx)
            await self._run_op(
                ctx,
                lambda t: self._service.apply_template(
                    guild_id=gid, actor_name=an, actor_id=aid, template_text=raw, confirm_token=t
                ),
                confirm=confirm,
            )

        @self.admin_command(
            name="export-server",
            description=t("adapter.cmd_export_server_desc"),
        )
        async def export_server(ctx):
            await ctx.defer(ephemeral=True)
            try:
                text = self._export_server_yaml(self._guild(self._ctx_guild(ctx)))
            except Exception:
                log.exception("export-server failed")
                await ctx.respond(t("adapter.export_error"), ephemeral=True)
                return
            import io

            fp = io.BytesIO(text.encode("utf-8"))
            await ctx.respond(
                t("adapter.export_file_msg_slash"),
                file=discord.File(fp, filename="server-template.yaml"),
                ephemeral=True,
            )

        @self.admin_command(name="kick", description="Kick a member (high-risk; needs confirm:true).")
        async def kick(ctx, user_id: str, confirm: bool = False):
            gid, (an, aid) = self._ctx_guild(ctx), self._actor(ctx)
            await self._run_op(
                ctx,
                lambda t: self._service.kick_member(
                    guild_id=gid, actor_name=an, actor_id=aid, user_id=int(user_id), confirm_token=t
                ),
                confirm=confirm,
            )

        @self.admin_command(name="ban", description="Ban a member (high-risk; needs confirm:true).")
        async def ban(ctx, user_id: str, confirm: bool = False):
            gid, (an, aid) = self._ctx_guild(ctx), self._actor(ctx)
            await self._run_op(
                ctx,
                lambda t: self._service.ban_member(
                    guild_id=gid, actor_name=an, actor_id=aid, user_id=int(user_id), confirm_token=t
                ),
                confirm=confirm,
            )

        @self.admin_command(name="timeout", description="Timeout a member (low-risk; immediate).")
        async def timeout_cmd(ctx, user_id: str, duration: int = 600):
            gid, (an, aid) = self._ctx_guild(ctx), self._actor(ctx)
            await self._run_op(
                ctx,
                lambda t: self._service.timeout_member(
                    guild_id=gid, actor_name=an, actor_id=aid, user_id=int(user_id), duration_seconds=duration
                ),
                confirm=False,
            )

        @self.admin_command(
            name="purge", description="Delete messages in a channel (high-risk; max 100; needs confirm:true)."
        )
        async def purge(ctx, channel_id: str, count: int, confirm: bool = False):
            gid, (an, aid) = self._ctx_guild(ctx), self._actor(ctx)
            await self._run_op(
                ctx,
                lambda t: self._service.purge_messages(
                    guild_id=gid, actor_name=an, actor_id=aid, channel_id=int(channel_id), count=count, confirm_token=t
                ),
                confirm=confirm,
            )

        @self.admin_command(name="set-admin-role", description=t("adapter.cmd_set_admin_role_desc"))
        async def set_admin_role(ctx, role_id: str):
            if self._settings is None:
                await ctx.respond(t("adapter.no_settings_store"), ephemeral=True)
                return
            try:
                rid = int(role_id)
            except ValueError:
                await ctx.respond(t("adapter.role_id_not_int"), ephemeral=True)
                return
            gid = self._ctx_guild(ctx)
            self._settings.set_admin_role(gid, rid)
            await ctx.respond(t("adapter.admin_role_set", rid=rid), ephemeral=True)

        @self.admin_command(name="set-welcome", description=t("adapter.cmd_set_welcome_desc"))
        async def set_welcome(ctx, channel_id: str, message: str = ""):
            if self._settings is None:
                await ctx.respond(t("adapter.no_settings_store"), ephemeral=True)
                return
            try:
                cid = int(channel_id)
            except ValueError:
                await ctx.respond(t("adapter.channel_id_not_int"), ephemeral=True)
                return
            gid = self._ctx_guild(ctx)
            self._settings.set_welcome_channel(gid, cid)
            if message:
                self._settings.set_welcome_message(gid, message)
            summary = t("adapter.welcome_channel_set", cid=cid)
            if message:
                summary += t("adapter.welcome_message_updated")
            await ctx.respond(summary, ephemeral=True)

        @self.admin_command(name="set-default-role", description=t("adapter.cmd_set_default_role_desc"))
        async def set_default_role(ctx, role_id: str):
            if self._settings is None:
                await ctx.respond(t("adapter.no_settings_store"), ephemeral=True)
                return
            try:
                rid = int(role_id)
            except ValueError:
                await ctx.respond(t("adapter.role_id_not_int"), ephemeral=True)
                return
            gid = self._ctx_guild(ctx)
            self._settings.set_default_role(gid, rid)
            await ctx.respond(t("adapter.default_role_set", rid=rid), ephemeral=True)

        @self.admin_command(name="show-config", description=t("adapter.cmd_show_config_desc"))
        async def show_config(ctx):
            if self._settings is None:
                await ctx.respond(t("adapter.no_settings_store"), ephemeral=True)
                return
            gid = self._ctx_guild(ctx)
            s = self._settings.get(gid)
            not_set = t("adapter.not_set")
            lines = [
                t("adapter.config_admin_role", value=s.admin_role_id or not_set),
                t("adapter.config_welcome_channel", value=s.welcome_channel_id or not_set),
                t("adapter.config_default_role", value=s.default_role_id or not_set),
                t("adapter.config_welcome_message", value=s.welcome_message or not_set),
            ]
            await ctx.respond("\n".join(lines), ephemeral=True)

        @self.admin_command(
            name="cleanup-report", description=t("adapter.cmd_cleanup_report_desc")
        )
        async def cleanup_report_cmd(ctx, days: int = 90):
            gid = self._ctx_guild(ctx)
            s = self._settings.get(gid) if self._settings else None
            await self._run_op(
                ctx,
                lambda t: self._service.cleanup_report(
                    guild_id=gid,
                    inactive_days=days,
                    admin_role_id=(int(s.admin_role_id or 0) if s else 0),
                    welcome_channel_id=(int(s.welcome_channel_id or 0) if s else 0),
                    protected_role_ids=self._reward_role_ids(gid),
                ),
                confirm=False,
            )

        @self.admin_command(
            name="cleanup-archive",
            description=t("adapter.cmd_cleanup_archive_desc"),
        )
        async def cleanup_archive_cmd(ctx, days: int = 90, confirm: bool = False):
            gid, (an, aid) = self._ctx_guild(ctx), self._actor(ctx)
            s = self._settings.get(gid) if self._settings else None
            await self._run_op(
                ctx,
                lambda t: self._service.cleanup_archive(
                    guild_id=gid,
                    actor_name=an,
                    actor_id=aid,
                    inactive_days=days,
                    admin_role_id=(int(s.admin_role_id or 0) if s else 0),
                    welcome_channel_id=(int(s.welcome_channel_id or 0) if s else 0),
                    protected_role_ids=self._reward_role_ids(gid),
                    confirm_token=t,
                ),
                confirm=confirm,
            )

        @self.admin_command(
            name="cleanup-purge",
            description=t("adapter.cmd_cleanup_purge_desc"),
        )
        async def cleanup_purge_cmd(ctx, days: int = 90, confirm: bool = False):
            gid, (an, aid) = self._ctx_guild(ctx), self._actor(ctx)
            s = self._settings.get(gid) if self._settings else None
            await self._run_op(
                ctx,
                lambda t: self._service.cleanup_purge(
                    guild_id=gid,
                    actor_name=an,
                    actor_id=aid,
                    inactive_days=days,
                    admin_role_id=(int(s.admin_role_id or 0) if s else 0),
                    welcome_channel_id=(int(s.welcome_channel_id or 0) if s else 0),
                    protected_role_ids=self._reward_role_ids(gid),
                    confirm_token=t,
                ),
                confirm=confirm,
            )

        @self.admin_command(
            name="announce-add",
            description=t("adapter.cmd_announce_add_desc"),
        )
        async def announce_add(
            ctx,
            channel: discord.Option(discord.TextChannel, t("adapter.opt_announce_channel")),
            message: str,
            cron: str,
            polish: bool = False,
        ):
            if self._announcements is None:
                await ctx.respond(t("adapter.no_announce_store"), ephemeral=True)
                return
            msg = message
            if polish and self._llm is not None and msg.strip():
                await ctx.defer(ephemeral=True)
                from ..service.copywriter import polish_copy

                msg = await polish_copy(self._llm, bot_name=self._bot_name, raw=msg, kind="announce")
            try:
                aid = self._announcements.add(
                    guild_id=self._ctx_guild(ctx),
                    channel_id=channel.id,
                    message=msg,
                    cron=cron.strip(),
                    created_by=self._actor(ctx)[1],
                )
            except Exception as exc:  # noqa: BLE001 - e.g. cron validation failure
                await ctx.respond(t("adapter.cron_invalid", exc=exc), ephemeral=True)
                return
            ack = t("adapter.announce_added", aid=aid, mention=channel.mention, cron=cron.strip())
            if polish and msg.strip():
                ack += t("adapter.polished_copy", msg=msg)
            await ctx.respond(ack, ephemeral=True)

        @self.admin_command(
            name="announce-add-once",
            description=t("adapter.cmd_announce_add_once_desc"),
        )
        async def announce_add_once(
            ctx,
            channel: discord.Option(discord.TextChannel, t("adapter.opt_announce_channel")),
            message: str,
            at: str,
            polish: bool = False,
        ):
            if self._announcements is None:
                await ctx.respond(t("adapter.no_announce_store"), ephemeral=True)
                return
            from datetime import datetime

            from ..service.announcements import KST

            try:
                run_at = (
                    datetime.strptime(at.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=KST).timestamp()
                )
            except ValueError:
                await ctx.respond(t("adapter.announce_time_format"), ephemeral=True)
                return
            msg = message
            if polish and self._llm is not None and msg.strip():
                await ctx.defer(ephemeral=True)
                from ..service.copywriter import polish_copy

                msg = await polish_copy(self._llm, bot_name=self._bot_name, raw=msg, kind="announce")
            aid = self._announcements.add(
                guild_id=self._ctx_guild(ctx),
                channel_id=channel.id,
                message=msg,
                run_at=run_at,
                created_by=self._actor(ctx)[1],
            )
            ack = t("adapter.announce_once_added", aid=aid, mention=channel.mention, at=at.strip())
            if polish and msg.strip():
                ack += t("adapter.polished_copy", msg=msg)
            await ctx.respond(ack, ephemeral=True)

        @self.admin_command(name="announce-list", description=t("adapter.cmd_announce_list_desc"))
        async def announce_list(ctx):
            if self._announcements is None:
                await ctx.respond(t("adapter.no_announce_store"), ephemeral=True)
                return
            items = self._announcements.list_for_guild(self._ctx_guild(ctx))
            if not items:
                await ctx.respond(t("adapter.no_announcements"), ephemeral=True)
                return
            lines = []
            for a in items:
                when = t("adapter.announce_cron_label", cron=a.cron) if a.cron else t("adapter.announce_once_label")
                state = "" if a.enabled else t("adapter.announce_disabled_label")
                lines.append(f"#{a.id} · <#{a.channel_id}> · {when}{state} · {a.message[:40]}")
            await ctx.respond("\n".join(lines)[:1900], ephemeral=True)

        @self.admin_command(name="announce-remove", description=t("adapter.cmd_announce_remove_desc"))
        async def announce_remove(ctx, ann_id: int):
            if self._announcements is None:
                await ctx.respond(t("adapter.no_announce_store"), ephemeral=True)
                return
            ok = self._announcements.remove(int(ann_id), self._ctx_guild(ctx))
            status = t("adapter.deleted_label") if ok else t("adapter.id_not_found")
            await ctx.respond(f"{status}: #{int(ann_id)}", ephemeral=True)

        @self.admin_command(name="announce-toggle", description=t("adapter.cmd_announce_toggle_desc"))
        async def announce_toggle(ctx, ann_id: int, enabled: bool):
            if self._announcements is None:
                await ctx.respond(t("adapter.no_announce_store"), ephemeral=True)
                return
            ok = self._announcements.set_enabled(int(ann_id), self._ctx_guild(ctx), enabled)
            status = "✅" if ok else t("adapter.id_not_found")
            state = t("adapter.on_label") if enabled else t("adapter.off_label")
            await ctx.respond(
                f"{status}: #{int(ann_id)} {state}", ephemeral=True
            )

        @self.admin_command(
            name="event-add",
            description=t("adapter.cmd_event_add_desc"),
        )
        async def event_add(
            ctx,
            channel: discord.Option(discord.TextChannel, t("adapter.opt_announce_channel")),
            title: str,
            at: str,
            leads: str = "",
            note: str = "",
            mention: str = "",
            polish: bool = False,
        ):
            if self._events is None:
                await ctx.respond(t("adapter.no_event_store"), ephemeral=True)
                return
            import time as _t
            from datetime import datetime

            from ..service.announcements import EVENT_DEFAULT_LEADS, KST

            try:
                event_at = (
                    datetime.strptime(at.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=KST).timestamp()
                )
            except ValueError:
                await ctx.respond(t("adapter.event_time_format"), ephemeral=True)
                return
            try:
                lead_days = (
                    tuple(sorted({int(x) for x in leads.split(",") if x.strip()}, reverse=True))
                    if leads.strip()
                    else EVENT_DEFAULT_LEADS
                )
                if not lead_days or any(d < 0 for d in lead_days):
                    raise ValueError
            except ValueError:
                await ctx.respond(t("adapter.leaddays_format"), ephemeral=True)
                return
            note_text = note.strip() or None
            if polish and self._llm is not None and note_text:
                await ctx.defer(ephemeral=True)
                from ..service.copywriter import polish_copy

                note_text = await polish_copy(
                    self._llm, bot_name=self._bot_name, raw=note_text, kind="event", title=title,
                )
            eid = self._events.add(
                guild_id=self._ctx_guild(ctx),
                channel_id=channel.id,
                title=title.strip(),
                event_at=event_at,
                lead_days=lead_days,
                message=note_text,
                mention=mention.strip() or None,
                created_by=self._actor(ctx)[1],
            )
            now = _t.time()
            upcoming = [d for d in lead_days if event_at - d * 86400 > now]
            up_tags = "/".join("D-DAY" if d == 0 else f"D-{d}" for d in upcoming)
            head = t("adapter.event_added_head", eid=eid, mention=channel.mention, title=title.strip(), at=at.strip())
            if event_at + 3600 < now:
                plan = t("adapter.event_past")
            elif any(event_at - d * 86400 <= now for d in lead_days):
                cur = max(0, round((event_at - now) / 86400))
                cur_tag = "D-DAY" if cur == 0 else f"D-{cur}"
                plan = t("adapter.event_send_now", cur_tag=cur_tag) + (t("adapter.event_then", up_tags=up_tags) if up_tags else "")
            else:
                plan = t("adapter.event_scheduled", up_tags=up_tags)
            ack = head + plan
            if polish and note_text:
                ack += t("adapter.polished_copy", msg=note_text)
            await ctx.respond(ack, ephemeral=True)

        @self.admin_command(name="event-list", description=t("adapter.cmd_event_list_desc"))
        async def event_list(ctx):
            if self._events is None:
                await ctx.respond(t("adapter.no_event_store"), ephemeral=True)
                return
            from datetime import datetime

            from ..service.announcements import KST

            items = self._events.list_for_guild(self._ctx_guild(ctx))
            if not items:
                await ctx.respond(t("adapter.no_events"), ephemeral=True)
                return
            lines = []
            for e in items:
                when = datetime.fromtimestamp(e.event_at, KST).strftime("%Y-%m-%d %H:%M")
                remaining = [d for d in e.lead_days if d not in e.fired_leads]
                tags = "/".join("DDAY" if d == 0 else f"D-{d}" for d in remaining) or t("adapter.event_done_label")
                state = "" if e.enabled else " ⏸️"
                lines.append(t("adapter.event_list_line", id=e.id, channel_id=e.channel_id, title=e.title, when=when, state=state, tags=tags))
            await ctx.respond("\n".join(lines)[:1900], ephemeral=True)

        @self.admin_command(name="event-remove", description=t("adapter.cmd_event_remove_desc"))
        async def event_remove(ctx, event_id: int):
            if self._events is None:
                await ctx.respond(t("adapter.no_event_store"), ephemeral=True)
                return
            ok = self._events.remove(int(event_id), self._ctx_guild(ctx))
            status = t("adapter.deleted_label") if ok else t("adapter.id_not_found")
            await ctx.respond(f"{status}: #{int(event_id)}", ephemeral=True)

        @self.admin_command(name="event-toggle", description=t("adapter.cmd_event_toggle_desc"))
        async def event_toggle(ctx, event_id: int, enabled: bool):
            if self._events is None:
                await ctx.respond(t("adapter.no_event_store"), ephemeral=True)
                return
            ok = self._events.set_enabled(int(event_id), self._ctx_guild(ctx), enabled)
            status = "✅" if ok else t("adapter.id_not_found")
            state = t("adapter.on_label") if enabled else t("adapter.off_label")
            await ctx.respond(
                f"{status}: #{int(event_id)} {state}", ephemeral=True
            )


    # --- Command helpers (ralplan S3): actor/guild extraction + confirm-then-execute render ---

    @staticmethod
    def _actor(ctx) -> tuple[str, int]:
        a = getattr(ctx, "author", None) or getattr(ctx, "user", None)
        return (getattr(a, "display_name", "?"), int(getattr(a, "id", 0)))

    def _ctx_guild(self, ctx) -> int:
        gid = getattr(ctx, "guild_id", 0) or getattr(getattr(ctx, "guild", None), "id", 0)
        return int(gid or self._guild_id)

    def _reward_role_ids(self, guild_id: int) -> set[int]:
        """Set of level-reward role ids — protects reward roles (even with 0 members) from being deleted as orphans by cleanup."""
        if self._leveling is None:
            return set()
        try:
            return {int(rid) for _, rid in self._leveling.list_role_rewards(guild_id)}
        except Exception:  # noqa: BLE001
            return set()

    def _resolve_category(self, guild, category_id):
        """Resolve a category id to a channel object; raise if given-but-unresolved (ralplan S6:
        no silent root/move fallback). Returns None when category_id is falsy."""
        if not category_id:
            return None
        category = guild.get_channel(int(category_id))
        if category is None:
            raise RuntimeError(f"category {category_id} not found")
        return category

    async def _run_op(self, ctx, factory, *, confirm: bool) -> None:
        """Run a service op and render the result (ralplan S3/S6). High-risk ops return
        needs_confirmation: confirm=true re-submits the issued single-use token (policy layer
        consumes + executes); otherwise a danger-styled confirm button is shown (both paths per
        R10). Errors are surfaced ephemerally instead of failing silently."""
        try:
            # Ack within Discord's 3s window; the actual work (bulk REST + rate-limit spacing)
            # then runs against the 15-min followup window instead of the initial deadline (S6).
            await ctx.defer(ephemeral=True)
            res = await factory(None)
            if res.needs_confirmation:
                if confirm:
                    res = await factory(res.confirmation_token)
                else:
                    view = _ConfirmView(factory, res.confirmation_token)
                    await ctx.respond(
                        t("adapter.danger_confirm_prompt", detail=res.detail),
                        view=view,
                        ephemeral=True,
                    )
                    return
            await ctx.respond(res.detail, ephemeral=True)
        except Exception:
            log.exception("admin command failed")
            await ctx.respond(
                t("adapter.op_error"),
                ephemeral=True,
            )

    # --- GuildAdmin primitives (ralplan S2; exercised by S3+ commands via mock guild) ---

    def _guild(self, guild_id: int):
        guild = self._client.get_guild(guild_id)
        if guild is None:
            raise RuntimeError(f"guild {guild_id} not found (bot not in guild / not cached)")
        return guild

    async def _member(self, guild_id: int, user_id: int):
        """Resolve a member id-only: cache first, then fetch_member REST fallback — works
        without the privileged members intent (ralplan S4 boundary contract)."""
        guild = self._guild(guild_id)
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound as exc:
                raise RuntimeError(f"member {user_id} not found") from exc
        return member

    async def create_category(self, guild_id: int, name: str, *, reason: str) -> str:
        await self._rl.acquire()
        category = await self._guild(guild_id).create_category(name, reason=reason)
        return str(category.id)

    async def create_channel(
        self, guild_id: int, name: str, kind: str, category_id: int | None = None, *, reason: str
    ) -> str:
        await self._rl.acquire()
        guild = self._guild(guild_id)
        category = self._resolve_category(guild, category_id)
        if kind == "voice":
            channel = await guild.create_voice_channel(name, category=category, reason=reason)
        else:
            channel = await guild.create_text_channel(name, category=category, reason=reason)
        return str(channel.id)

    async def edit_channel(
        self,
        guild_id: int,
        channel_id: int,
        *,
        name: str | None = None,
        category_id: int | None = None,
        reason: str,
    ) -> None:
        guild = self._guild(guild_id)
        channel = guild.get_channel(channel_id)
        if channel is None:
            raise RuntimeError(f"channel {channel_id} not found")
        fields: dict = {}
        if name is not None:
            fields["name"] = name
        if category_id is not None:
            fields["category"] = self._resolve_category(guild, category_id)
        await channel.edit(reason=reason, **fields)

    async def delete_channel(self, guild_id: int, channel_id: int, *, reason: str) -> None:
        guild = self._guild(guild_id)
        channel = guild.get_channel(channel_id)
        if channel is None:
            raise RuntimeError(f"channel {channel_id} not found")
        await channel.delete(reason=reason)

    async def create_role(
        self, guild_id: int, name: str, *, permissions: int = 0, reason: str
    ) -> str:
        await self._rl.acquire()
        role = await self._guild(guild_id).create_role(
            name=name, permissions=discord.Permissions(permissions), reason=reason
        )
        return str(role.id)

    async def delete_role(self, guild_id: int, role_id: int, *, reason: str) -> None:
        await self._rl.acquire()
        role = self._guild(guild_id).get_role(role_id)
        if role is None:
            raise RuntimeError(f"role {role_id} not found")
        await role.delete(reason=reason)

    async def role_permissions(self, guild_id: int, role_id: int) -> int:
        role = self._guild(guild_id).get_role(role_id)
        if role is None:
            raise RuntimeError(f"role {role_id} not found")
        return role.permissions.value

    async def assign_role(self, guild_id: int, user_id: int, role_id: int, *, reason: str) -> None:
        role = self._guild(guild_id).get_role(role_id)
        if role is None:
            raise RuntimeError(f"role {role_id} not found")
        member = await self._member(guild_id, user_id)
        await member.add_roles(role, reason=reason)

    async def remove_role(self, guild_id: int, user_id: int, role_id: int, *, reason: str) -> None:
        role = self._guild(guild_id).get_role(role_id)
        if role is None:
            raise RuntimeError(f"role {role_id} not found")
        member = await self._member(guild_id, user_id)
        await member.remove_roles(role, reason=reason)

    async def set_role_permissions(
        self, guild_id: int, role_id: int, permissions: int, *, reason: str
    ) -> None:
        role = self._guild(guild_id).get_role(role_id)
        if role is None:
            raise RuntimeError(f"role {role_id} not found")
        await role.edit(permissions=discord.Permissions(permissions), reason=reason)

    async def set_channel_role_overwrite(
        self, guild_id: int, channel_id: int, role_id: int, *, view: bool, reason: str
    ) -> None:
        await self._rl.acquire()
        guild = self._guild(guild_id)
        channel = guild.get_channel(channel_id)
        if channel is None:
            raise RuntimeError(f"channel {channel_id} not found")
        target = guild.get_role(role_id)
        if target is None:
            raise RuntimeError(f"role {role_id} not found")
        await channel.set_permissions(
            target, overwrite=discord.PermissionOverwrite(view_channel=view), reason=reason
        )

    async def kick_member(self, guild_id: int, user_id: int, *, reason: str) -> None:
        member = await self._member(guild_id, user_id)
        await member.kick(reason=reason)

    async def ban_member(self, guild_id: int, user_id: int, *, reason: str) -> None:
        member = await self._member(guild_id, user_id)
        await member.ban(reason=reason)

    async def timeout_member(
        self, guild_id: int, user_id: int, duration_seconds: int, *, reason: str
    ) -> None:
        from datetime import timedelta
        member = await self._member(guild_id, user_id)
        await member.timeout(timedelta(seconds=duration_seconds), reason=reason)

    async def purge_messages(
        self, guild_id: int, channel_id: int, count: int, *, reason: str
    ) -> int:
        guild = self._guild(guild_id)
        channel = guild.get_channel(channel_id)
        if channel is None:
            raise RuntimeError(f"channel {channel_id} not found")
        deleted = await channel.purge(limit=count, reason=reason)
        return len(deleted)

    async def list_roles(self, guild_id: int) -> list[dict]:
        return [
            {
                "id": str(r.id),
                "name": r.name,
                "member_count": len(r.members),
                "managed": bool(r.managed),
                "is_default": r.is_default(),
            }
            for r in self._guild(guild_id).roles
        ]

    async def list_channels(self, guild_id: int) -> list[dict]:
        out: list[dict] = []
        for ch in self._guild(guild_id).channels:
            parent = getattr(ch, "category_id", None)
            lmid = getattr(ch, "last_message_id", None)
            ow_roles = [
                str(t.id) for t in getattr(ch, "overwrites", {}) if isinstance(t, discord.Role)
            ]
            out.append(
                {
                    "id": str(ch.id),
                    "name": ch.name,
                    "type": int(ch.type.value),
                    "parent_id": str(parent) if parent else None,
                    "last_message_id": str(lmid) if lmid else None,
                    "overwrite_role_ids": ow_roles,
                }
            )
        return out

    # --- ChatPlatform interface (channel_id forms, used by non-Discord callers) ---

    async def recent_messages(self, channel_id: str, n: int) -> list[BufferedMessage]:
        channel = self._client.get_channel(int(channel_id))
        if channel is None:
            return []
        return await self._history(channel, n)

    async def send(self, channel_id: str, text: str) -> None:
        channel = self._client.get_channel(int(channel_id))
        if channel is None:
            log.warning("send: channel %s not found", channel_id)
            return
        await self._send_to(channel, text)

    async def run(self) -> None:
        await self._client.start(self._token)


class _ConfirmView(discord.ui.View):
    """High-risk confirmation button (ralplan S6 / R10). Holds the issued single-use token + the
    op factory; clicking re-runs the op with the token (consumed in the policy layer)."""

    def __init__(self, factory, token: str, *, authorized=None, timeout: float = 300.0) -> None:
        super().__init__(timeout=timeout)
        self._factory = factory
        self._token = token
        self._authorized = authorized  # NL public message: re-check the button clicker's permission too (required)

    async def _do_confirm(self, interaction) -> None:
        if self._authorized is not None and not self._authorized(interaction):
            await interaction.response.send_message(
                t("adapter.confirm_no_permission"), ephemeral=True
            )
            return
        await interaction.response.defer()  # ack the button click before the (possibly bulk) op
        try:
            res = await self._factory(self._token)
            msg = res.detail
        except Exception:
            log.exception("admin confirm failed")
            msg = t("adapter.confirm_error")
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(content=msg, view=self)
        self.stop()

    @discord.ui.button(label=t("adapter.confirm_button"), style=discord.ButtonStyle.danger)
    async def confirm(self, button, interaction) -> None:
        await self._do_confirm(interaction)
