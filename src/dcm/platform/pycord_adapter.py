from __future__ import annotations

import asyncio
import logging
import time

import discord

from .base import (
    BufferedMessage,
    ChatPlatform,
    GuildAdmin,
    IncomingMessage,
    MentionHandler,
    is_admin,
)
from ..service import guild_admin as policy

log = logging.getLogger(__name__)

DISCORD_MAX = 2000

# 봇 이름으로 호명할 때 붙는 한국어 호격 조사 (썩스가재야/썩스가재님/썩스가재씨).
_VOCATIVE = "야아님씨"
# 이름만으로 부를 때 이름 뒤에 올 수 있는 경계 문자 ("썩스가재 안녕", "썩스가재!", "썩스가재?").
_NAME_BOUNDARY = " \t\n\r!?.,~…:;"

# 서버 템플릿 첨부 처리 (NL 경로): 허용 확장자 / 최대 크기 / 셋업 의도 키워드.
_TEMPLATE_EXTS = (".yaml", ".yml", ".json")
_TEMPLATE_MAX_BYTES = 256 * 1024
_SETUP_INTENT_WORDS = (
    "세팅", "셋업", "set up", "setup", "적용", "템플릿", "template",
    "구성", "세트업", "블루프린트", "blueprint", "만들어",
)

# 서버 구조 → YAML export (NL 경로) 의도 판정: 형식어 + 액션어를 동시에 만족할 때만.
_EXPORT_FORMAT_WORDS = ("yaml", "json", "템플릿", "블루프린트", "blueprint")
_EXPORT_ACTION_WORDS = ("뽑", "추출", "내보내", "백업", "스냅샷", "snapshot", "export", "dump", "확인", "보여", "구조")


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
    """Pycord implementation of ChatPlatform (DESIGN.md §3). The only module that imports discord."""

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
    ) -> None:
        self._token = token
        self._bot_name = bot_name
        self._buffer_size = buffer_size
        self._cooldown = cooldown_seconds
        self._guild_id = guild_id  # guild for guild-scoped slash registration (ralplan S1/S2)
        self._admin_role_id = admin_role_id  # designated ADMIN_ROLE for the InvokerCheck (S2)
        self._last_seen: dict[str, float] = {}  # author_id → monotonic ts (cooldown, §14.4)
        self._handler: MentionHandler | None = None
        self._service = None  # GuildAdmin policy service, wired in register_admin_commands (S2)
        self._pending = policy.PendingConfirmations()  # adapter-local confirm-token carry (S2)
        self._admin_commands: list[str] = []  # registry for the by-construction InvokerCheck test
        self._rl = policy.RateLimiter()  # additive burst-smoothing over pycord 429 handling (S6)
        self._onboarding = onboarding_policy  # OnboardingPolicy 인스턴스 (S6 온보딩)
        self._settings = guild_settings  # per-guild 설정/관리역할 (멀티길드 v2); None이면 env 시드값 폴백
        self._public_commands: list[str] = []  # 비가드 멤버 공개 명령 registry (leveling 표시)
        self._leveling = None  # LevelingService, register_leveling_commands 에서 주입
        self._announcements = announcements  # 예약 공지 저장소(AnnouncementStore); None이면 비활성
        self._events = events  # 행사 카운트다운 공지 저장소(EventStore); None이면 비활성
        self._announce_task = None

        intents = discord.Intents.default()
        # Privileged — must also be enabled in the Developer Portal (DESIGN.md §14.2).
        intents.message_content = True
        intents.members = True  # on_member_join 발화에 필요 (S6 온보딩; Developer Portal에서도 활성화 필요)
        # discord.Bot (subclass of Client) so application/slash commands register & auto-sync.
        # Bot preserves the inherited mention path (on_message/get_channel/start/user) — ralplan S1.
        self._client = discord.Bot(intents=intents)
        self._client.event(self.on_ready)
        self._client.event(self.on_message)
        self._client.event(self.on_member_join)

    def on_mention(self, handler: MentionHandler) -> None:
        self._handler = handler

    @property
    def pending(self):
        """Adapter-local pending high-risk confirmations (shared with the policy service, S2)."""
        return self._pending

    async def on_ready(self) -> None:
        log.info("%s online as %s", self._bot_name, self._client.user)
        # cutover 확인용: privileged intent 활성 여부를 운영자가 로그에서 검증(DESIGN.md §14.2, S7).
        intents = self._client.intents
        log.info("privileged intent message_content=%s", intents.message_content)
        log.info("privileged intent members=%s", intents.members)
        if (self._announcements is not None or self._events is not None) and self._announce_task is None:
            self._announce_task = asyncio.create_task(self._announce_loop())
            log.info("scheduled-announcement loop started")

    async def _announce_loop(self) -> None:
        """매분 예약 공지를 확인해 발화(예외 침묵 degrade). on_ready 에서 1회 시작."""
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
                            if ann.run_at is not None:  # 1회성 → 발화 후 비활성화
                                self._announcements.set_enabled(ann.id, ann.guild_id, False)
                        except Exception:  # noqa: BLE001 - 개별 공지 실패는 침묵, 루프 유지
                            log.exception("announcement %s fire failed", ann.id)
                if self._events is not None:
                    for evt in self._events.list_enabled():
                        try:
                            leads = due_event_leads(evt, now)
                            if leads:
                                # 라벨은 실제 남은 일수 기준(늦게 등록/밀린 경우도 정확한 D-N 표시).
                                days = max(0, round((evt.event_at - now) / 86400))
                                channel = self._client.get_channel(int(evt.channel_id))
                                if channel is not None:
                                    await self._send_to(channel, render_event_message(evt, days))
                                for lead in leads:  # 발화분 + 밀린 과거분 모두 마킹
                                    self._events.mark_lead_fired(evt.id, lead)
                            if now > evt.event_at + 3600:  # 행사 종료 → 비활성화
                                self._events.set_enabled(evt.id, evt.guild_id, False)
                        except Exception:  # noqa: BLE001 - 개별 행사 실패는 침묵, 루프 유지
                            log.exception("event %s fire failed", evt.id)
            except Exception:  # noqa: BLE001
                log.exception("announcement loop tick failed")
            await asyncio.sleep(60)

    async def on_message(self, message: discord.Message) -> None:
        # Ignore self and other bots.
        if message.author.bot or message.author == self._client.user:
            return
        # DM(길드 없음) 전면 무시 — 멀티길드 격리상 guild_id 없는 경로는 fail-closed (P2 / G5).
        if message.guild is None:
            return

        # 활동 레벨링: 모든 비봇 메시지에 XP 적립 (인메모리 쿨다운+설정 TTL 캐시 → steady-state DB read 0, 실패 침묵).
        # _is_addressed 이전에 둬서 멘션이 아닌 일반 메시지도 집계한다 (P1).
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
                # 적립 시(쿨다운 통과)에만 레벨→역할 무인 부여 시도(멱등·allow-list 가드, G004).
                asyncio.create_task(self._safe_reconcile(message.author))
        # 호출 감지: @멘션 / 봇 메시지 reply / 메시지 이름 호명("썩스가재야 …", "안녕 썩스가재야").
        # 이름 호명은 스팸을 억제하면서 자연스러운 호명을 허용 (DESIGN.md §14.4; persona.md 예시).
        if not self._is_addressed(message):
            return

        # Per-user cooldown to curb spam / cost (DESIGN.md §14.4).
        author_id = str(message.author.id)
        now = time.monotonic()
        last = self._last_seen.get(author_id)
        if last is not None and now - last < self._cooldown:
            return
        self._last_seen[author_id] = now

        # 서버 템플릿 첨부(.yaml/.json) + 셋업 의도 → 운영진/주인 한정 템플릿 적용 경로로 분기.
        attachment = self._template_attachment(message)
        if attachment is not None and self._wants_setup(self._strip_mentions(message)):
            await self._handle_template_attachment(message, attachment)
            return

        # 첨부 없이 "서버 구조 yaml로 뽑아줘" → 운영진/주인 한정 현재 구조 export.
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
            await self._send_to(message.channel, reply)

    async def on_member_join(self, member) -> None:
        """신규 멤버 입장 이벤트 핸들러 (S6 온보딩).

        OnboardingPolicy.decide()로 동작을 결정한 뒤:
        - welcome_channel_id가 설정돼 있으면 해당 채널에 welcome_text를 전송한다.
        - default_role_id가 설정돼 있으면 멤버에게 해당 역할을 부여한다.
        예외는 log.exception으로 기록하되 봇이 죽지 않도록 침묵 degrade한다.
        """
        if self._onboarding is None:
            return
        if getattr(member, "bot", False):
            return  # 봇 입장에는 온보딩하지 않음 (S6)

        try:
            gid = getattr(getattr(member, "guild", None), "id", None)
            decision = self._onboarding.decide(member.display_name, guild_id=gid)
        except Exception:
            log.exception("on_member_join: 온보딩 결정 실패 (침묵 degrade)")
            return

        # 환영 메시지와 역할 부여를 독립 try로 분리 — 한쪽 실패가 다른 쪽을 막지 않게 한다.
        if decision.welcome_channel_id is not None and decision.welcome_text is not None:
            try:
                channel = self._client.get_channel(decision.welcome_channel_id)
                if channel is not None:
                    await channel.send(decision.welcome_text)
                else:
                    log.warning(
                        "on_member_join: welcome 채널 %d을 찾을 수 없음",
                        decision.welcome_channel_id,
                    )
            except Exception:
                log.exception("on_member_join: welcome 메시지 전송 실패 (침묵 degrade)")

        if decision.default_role_id is not None:
            try:
                guild = getattr(member, "guild", None)
                if guild is not None:
                    role = guild.get_role(decision.default_role_id)
                    if role is not None:
                        await member.add_roles(role, reason="온보딩 자동 역할 부여 (S6)")
                    else:
                        log.warning(
                            "on_member_join: default_role %d을 찾을 수 없음",
                            decision.default_role_id,
                        )
            except Exception:
                log.exception("on_member_join: 자동 역할 부여 실패 (침묵 degrade)")

    def _is_addressed(self, message: discord.Message) -> bool:
        """메시지가 봇을 향한 것인지 판정: @멘션 / 봇 메시지 reply / 이름 호명."""
        # 1) 직접 @멘션 (@everyone/@here는 message.mentions에 안 들어옴).
        if self._client.user in message.mentions:
            return True
        # 2) 봇이 보낸 메시지에 대한 답글(reply).
        ref = getattr(message, "reference", None)
        resolved = getattr(ref, "resolved", None) if ref is not None else None
        if resolved is not None and getattr(resolved, "author", None) == self._client.user:
            return True
        # 3) 이름으로 호명.
        return self._name_called(message.content)

    def _name_called(self, content: str) -> bool:
        """봇 이름으로 부르는 호명이면 True.

        "썩스가재"/"썩스가재야"/"썩스가재님 …"/"썩스가재 안녕"/"안녕 썩스가재야"는 매칭하고,
        "썩스가재가/썩스가재는" 같은 3인칭 언급은 제외한다 (스팸 가드).
        """
        name = (self._bot_name or "").strip()
        text = (content or "").strip()
        if not name or not text:
            return False
        # (a) 첫머리 호명: 이름 뒤가 끝/호격조사/경계문자.
        if text.startswith(name):
            rest = text[len(name):]
            if rest == "" or rest[0] in _VOCATIVE or rest[0] in _NAME_BOUNDARY:
                return True
        # (b) 독립 토큰 "이름+호격조사"가 문장 어디에든("안녕 썩스가재야").
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

    def _template_attachment(self, message):
        """메시지의 첫 번째 서버 템플릿 첨부(.yaml/.yml/.json)를 반환, 없으면 None."""
        for att in getattr(message, "attachments", None) or []:
            fn = (getattr(att, "filename", "") or "").lower()
            if fn.endswith(_TEMPLATE_EXTS):
                return att
        return None

    @staticmethod
    def _wants_setup(text: str) -> bool:
        """셋업/적용 의도 키워드가 있으면 True (첨부를 템플릿으로 처리할지 판단)."""
        low = (text or "").lower()
        return any(w in low for w in _SETUP_INTENT_WORDS)

    async def _handle_template_attachment(self, message, attachment) -> None:
        """'썩스가재야 + 템플릿 첨부 + 세팅' NL 경로: 운영진/주인만 미리보기→확인버튼으로 적용.

        파일 내용은 오직 apply_template 파서로만 전달(데이터 전용) — 기억/페르소나에 들어가지 않음.
        공개 메시지의 확인 버튼은 클릭자 권한을 다시 검사한다.
        """
        author = message.author
        if not self._is_admin(message):
            await self._send_to(
                message.channel, "⛔ 서버 템플릿 적용은 운영진(관리자 역할)이나 서버 주인만 할 수 있어."
            )
            return
        if self._service is None:
            await self._send_to(message.channel, "지금은 서버 관리 기능이 꺼져 있어.")
            return
        if int(getattr(attachment, "size", 0) or 0) > _TEMPLATE_MAX_BYTES:
            await self._send_to(message.channel, "템플릿 파일이 너무 커 (최대 256KB).")
            return
        try:
            raw = (await attachment.read()).decode("utf-8")
        except Exception:
            await self._send_to(
                message.channel, "템플릿 파일을 UTF-8 텍스트로 읽을 수 없어 (.yaml/.yml/.json)."
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
            res = await factory(None)  # 드라이런: 파싱 + 미리보기 (생성 없음)
        except Exception:
            log.exception("template dry-run failed")
            await self._send_to(message.channel, "템플릿 처리 중 오류가 났어.")
            return
        if not res.needs_confirmation:
            await self._send_to(message.channel, res.detail)  # 파싱 오류 또는 즉시 결과
            return

        def authorized(interaction) -> bool:
            return self._is_admin(interaction)

        view = _ConfirmView(factory, res.confirmation_token, authorized=authorized)
        await message.channel.send(
            f"{res.detail}\n\n적용하려면 아래 '확인 실행' 버튼을 눌러줘 (운영진/주인만 가능).",
            view=view,
        )

    @staticmethod
    def _wants_export(text: str) -> bool:
        """'서버 구조를 yaml로' 같은 export 의도면 True (형식어 + 액션어 동시 충족)."""
        low = (text or "").lower()
        return any(w in low for w in _EXPORT_FORMAT_WORDS) and any(
            w in low for w in _EXPORT_ACTION_WORDS
        )

    @staticmethod
    def _overwrite_info(channel, default_role, role_set):
        """채널 권한 오버라이트에서 (private, 열람 허용 역할이름들) 추출."""
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
        """현재 길드 구조를 ServerTemplate로 추출 (역할/카테고리/채널 + 비공개 감지)."""
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
        role_set = role_objs  # 리스트 멤버십(==)으로 검사 — discord.Role/테스트 더블 모두 안전(해시 불필요)
        roles = []
        for r in reversed(role_objs):  # 높은 역할 먼저
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
        """길드 → YAML 텍스트 (헤더 주석 포함). 무카테고리 최상위 채널은 주석으로 안내."""
        from ..service.template import to_yaml

        body = to_yaml(self._snapshot_template(guild))
        orphans = [
            c.name
            for c in getattr(guild, "channels", [])
            if getattr(c, "category", None) is None
            and getattr(c, "type", None) in (discord.ChannelType.text, discord.ChannelType.voice)
        ]
        header = "# dcm가 추출한 현재 서버 구조. 수정 후 /setup-server 로 다시 적용할 수 있어요.\n"
        if orphans:
            header += "# 참고: 카테고리에 없는 최상위 채널은 템플릿에서 제외됨 — " + ", ".join(orphans) + "\n"
        return header + body

    async def _handle_export(self, message) -> None:
        """'썩스가재야 서버 구조 yaml로 뽑아줘' NL 경로: 운영진/주인만 현재 구조를 YAML 파일로 회신."""
        if not self._is_admin(message):
            await self._send_to(
                message.channel, "⛔ 서버 구조 내보내기는 운영진(관리자 역할)이나 서버 주인만 할 수 있어."
            )
            return
        guild = getattr(message, "guild", None)
        if guild is None:
            await self._send_to(message.channel, "서버 안에서만 쓸 수 있어.")
            return
        try:
            text = self._export_server_yaml(guild)
        except Exception:
            log.exception("export failed")
            await self._send_to(message.channel, "서버 구조를 내보내는 중 오류가 났어.")
            return
        import io

        fp = io.BytesIO(text.encode("utf-8"))
        await message.channel.send(
            "현재 서버 구조 템플릿이야. 수정해서 다시 세팅에 쓸 수 있어.",
            file=discord.File(fp, filename="server-template.yaml"),
        )

    # --- InvokerCheck + by-construction admin-command registration (ralplan S2) ---

    @staticmethod
    def _role_ids(member) -> frozenset[int]:
        """Resolve a Discord member's role ids to plain data (ralplan S2)."""
        return frozenset(int(r.id) for r in getattr(member, "roles", None) or [])

    @staticmethod
    def _has_manage_guild(member) -> bool:
        """호출자가 디스코드 Manage Guild 또는 Administrator 권한 보유 여부 (어댑터 한정 해석)."""
        perms = getattr(member, "guild_permissions", None)
        if perms is None:
            return False
        return bool(getattr(perms, "manage_guild", False) or getattr(perms, "administrator", False))

    def _guild_admin_role(self, guild_id) -> int:
        """이 길드의 설정 관리역할 id (없으면 0 → authz 가 has_manage_guild 폴백). 멀티길드 v2.
        settings 미주입(단일길드 호환)이면 env 시드 admin_role_id 사용."""
        if self._settings is None:
            return int(self._admin_role_id or 0)
        try:
            return int(self._settings.get(int(guild_id)).admin_role_id or 0)
        except Exception:
            return 0

    @staticmethod
    def _is_owner(obj) -> bool:
        """길드 주인이면 True. 메시지(on_message)와 슬래시 ctx 모두에서 동작.

        디스코드 길드 owner는 본래 모든 권한을 가지므로 지정 역할과 무관하게 관리자로 취급한다.
        DM 등 guild가 없으면 False.
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
        (ralplan S2 / 멀티길드 v2 P4). Registered globally (no guild_ids) so the command appears
        in every server the bot inhabits. The wrapped callback runs only after the InvokerCheck
        passes; non-admins get an ephemeral denial."""
        import functools
        import inspect

        def decorator(func):
            @functools.wraps(func)
            async def wrapped(ctx, *args, **kw):
                if not self._is_admin(ctx):
                    await ctx.respond("지정된 관리자 역할 보유자만 사용할 수 있어.", ephemeral=True)
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
        non-ephemeral, no admin guard so every member can view levels (스펙 f5/f9)."""
        self._leveling = leveling_service

        @self.public_command(name="rank", description="내 활동 레벨과 XP를 봅니다.")
        async def rank(ctx):
            gid = self._ctx_guild(ctx)
            user = getattr(ctx, "author", None) or getattr(ctx, "user", None)
            display = getattr(user, "display_name", None) or str(getattr(user, "id", "?"))
            embed = leveling_service.rank_embed(gid, getattr(user, "id", 0), display)
            await ctx.respond(embed=embed)

        @self.public_command(
            name="leaderboard", description="서버 활동 리더보드 상위 순위를 봅니다."
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
            description="레벨 도달 시 자동 부여할 역할을 매핑합니다(관리자).",
        )
        async def set_level_role(
            ctx,
            level: discord.Option(int, "임계 레벨"),
            role: discord.Option(discord.Role, "부여할 역할"),
        ):
            gid = self._ctx_guild(ctx)
            guild = getattr(ctx, "guild", None)
            ok, reason = leveling_service.validate_reward_role(role, guild)
            if not ok:
                await ctx.respond(
                    f"그 역할은 자동부여로 안전하지 않아 거부했어 (사유: {reason}). "
                    "권한 없는 장식용 역할을 골라줘.",
                    ephemeral=True,
                )
                return
            leveling_service.set_role_reward(gid, int(level), int(role.id))
            await ctx.respond(
                f"레벨 {int(level)} 도달 시 '{getattr(role, 'name', role.id)}' 역할을 자동으로 줄게.",
                ephemeral=True,
            )

        @self.admin_command(
            name="remove-level-role", description="레벨→역할 매핑을 제거합니다(관리자)."
        )
        async def remove_level_role(ctx, level: discord.Option(int, "임계 레벨")):
            gid = self._ctx_guild(ctx)
            leveling_service.remove_role_reward(gid, int(level))
            await ctx.respond(f"레벨 {int(level)} 역할 매핑을 제거했어.", ephemeral=True)

        @self.admin_command(
            name="list-level-roles", description="레벨→역할 매핑 목록을 봅니다(관리자)."
        )
        async def list_level_roles(ctx):
            gid = self._ctx_guild(ctx)
            rewards = leveling_service.list_role_rewards(gid)
            if not rewards:
                await ctx.respond("아직 등록된 레벨→역할 매핑이 없어.", ephemeral=True)
                return
            lines = "\n".join(f"- 레벨 {lv} → <@&{rid}>" for lv, rid in rewards)
            await ctx.respond(f"레벨→역할 매핑:\n{lines}", ephemeral=True)

    async def _safe_reconcile(self, member) -> None:
        """레벨→역할 무인 부여를 백그라운드로 안전 실행(예외 침묵 degrade)."""
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
                "확인됐어. 너는 지정된 관리자 역할 보유자라 서버 관리 명령을 쓸 수 있어.",
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
                await ctx.respond("kind는 text 또는 voice여야 해.", ephemeral=True)
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
            description="YAML/JSON 템플릿(첨부)으로 역할·카테고리·채널 일괄 셋업 (고위험).",
        )
        async def setup_server(ctx, template: discord.Attachment, confirm: bool = False):
            try:
                raw = (await template.read()).decode("utf-8")
            except UnicodeDecodeError:
                await ctx.respond(
                    "템플릿 파일을 UTF-8로 읽을 수 없어 (.yaml/.yml/.json, UTF-8 인코딩).",
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
            description="현재 서버 구조(역할·카테고리·채널)를 YAML 템플릿으로 내보내기.",
        )
        async def export_server(ctx):
            await ctx.defer(ephemeral=True)
            try:
                text = self._export_server_yaml(self._guild(self._ctx_guild(ctx)))
            except Exception:
                log.exception("export-server failed")
                await ctx.respond("서버 구조를 내보내는 중 오류가 났어.", ephemeral=True)
                return
            import io

            fp = io.BytesIO(text.encode("utf-8"))
            await ctx.respond(
                "현재 서버 구조 템플릿이야. 수정해서 `/setup-server`로 다시 쓸 수 있어.",
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

        @self.admin_command(name="set-admin-role", description="이 서버의 관리자 역할을 설정한다.")
        async def set_admin_role(ctx, role_id: str):
            if self._settings is None:
                await ctx.respond("서버 설정 저장소가 없어.", ephemeral=True)
                return
            try:
                rid = int(role_id)
            except ValueError:
                await ctx.respond("role_id 가 올바른 정수가 아니야.", ephemeral=True)
                return
            gid = self._ctx_guild(ctx)
            self._settings.set_admin_role(gid, rid)
            await ctx.respond(f"이 서버 관리 역할을 {rid}로 설정했어.", ephemeral=True)

        @self.admin_command(name="set-welcome", description="환영 채널과 메시지를 설정한다.")
        async def set_welcome(ctx, channel_id: str, message: str = ""):
            if self._settings is None:
                await ctx.respond("서버 설정 저장소가 없어.", ephemeral=True)
                return
            try:
                cid = int(channel_id)
            except ValueError:
                await ctx.respond("channel_id 가 올바른 정수가 아니야.", ephemeral=True)
                return
            gid = self._ctx_guild(ctx)
            self._settings.set_welcome_channel(gid, cid)
            if message:
                self._settings.set_welcome_message(gid, message)
            summary = f"환영 채널을 {cid}로 설정했어."
            if message:
                summary += " 환영 메시지도 업데이트했어."
            await ctx.respond(summary, ephemeral=True)

        @self.admin_command(name="set-default-role", description="신규 멤버에게 자동 부여할 역할을 설정한다.")
        async def set_default_role(ctx, role_id: str):
            if self._settings is None:
                await ctx.respond("서버 설정 저장소가 없어.", ephemeral=True)
                return
            try:
                rid = int(role_id)
            except ValueError:
                await ctx.respond("role_id 가 올바른 정수가 아니야.", ephemeral=True)
                return
            gid = self._ctx_guild(ctx)
            self._settings.set_default_role(gid, rid)
            await ctx.respond(f"신규 멤버 기본 역할을 {rid}로 설정했어.", ephemeral=True)

        @self.admin_command(name="show-config", description="이 서버의 현재 설정을 표시한다.")
        async def show_config(ctx):
            if self._settings is None:
                await ctx.respond("서버 설정 저장소가 없어.", ephemeral=True)
                return
            gid = self._ctx_guild(ctx)
            s = self._settings.get(gid)
            lines = [
                f"관리자 역할: {s.admin_role_id or '미설정'}",
                f"환영 채널: {s.welcome_channel_id or '미설정'}",
                f"기본 역할: {s.default_role_id or '미설정'}",
                f"환영 메시지: {s.welcome_message or '미설정'}",
            ]
            await ctx.respond("\n".join(lines), ephemeral=True)

        @self.admin_command(
            name="cleanup-report", description="아카이브/삭제 예정 채널·역할을 미리 본다(변경 없음)."
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
            description="비활성 채널을 📦 아카이브 카테고리로 이동(숨김; 되돌림 가능; 확인 필요).",
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
            description="📦 아카이브 안 채널 + 고아 역할을 영구 삭제(되돌릴 수 없음; 확인 필요).",
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
            description="반복 공지 예약 (cron, KST). 예: '0 9 * * 1' = 매주 월 09:00.",
        )
        async def announce_add(
            ctx,
            channel: discord.Option(discord.TextChannel, "공지 채널"),
            message: str,
            cron: str,
        ):
            if self._announcements is None:
                await ctx.respond("예약 공지 저장소가 없어.", ephemeral=True)
                return
            try:
                aid = self._announcements.add(
                    guild_id=self._ctx_guild(ctx),
                    channel_id=channel.id,
                    message=message,
                    cron=cron.strip(),
                    created_by=self._actor(ctx)[1],
                )
            except Exception as exc:  # noqa: BLE001 - cron 검증 실패 등
                await ctx.respond(f"cron 이 올바르지 않아: {exc}", ephemeral=True)
                return
            await ctx.respond(
                f"✅ 반복 공지 #{aid} 등록: {channel.mention} 에 `{cron.strip()}` (KST)", ephemeral=True
            )

        @self.admin_command(
            name="announce-add-once",
            description="1회성 공지 예약. 시각은 KST 'YYYY-MM-DD HH:MM'.",
        )
        async def announce_add_once(
            ctx,
            channel: discord.Option(discord.TextChannel, "공지 채널"),
            message: str,
            at: str,
        ):
            if self._announcements is None:
                await ctx.respond("예약 공지 저장소가 없어.", ephemeral=True)
                return
            from datetime import datetime

            from ..service.announcements import KST

            try:
                run_at = (
                    datetime.strptime(at.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=KST).timestamp()
                )
            except ValueError:
                await ctx.respond("시각 형식이 틀려. 예: 2026-07-15 10:00 (KST)", ephemeral=True)
                return
            aid = self._announcements.add(
                guild_id=self._ctx_guild(ctx),
                channel_id=channel.id,
                message=message,
                run_at=run_at,
                created_by=self._actor(ctx)[1],
            )
            await ctx.respond(
                f"✅ 1회 공지 #{aid} 등록: {channel.mention} 에 {at.strip()} (KST)", ephemeral=True
            )

        @self.admin_command(name="announce-list", description="이 서버의 예약 공지 목록.")
        async def announce_list(ctx):
            if self._announcements is None:
                await ctx.respond("예약 공지 저장소가 없어.", ephemeral=True)
                return
            items = self._announcements.list_for_guild(self._ctx_guild(ctx))
            if not items:
                await ctx.respond("등록된 예약 공지가 없어.", ephemeral=True)
                return
            lines = []
            for a in items:
                when = f"cron `{a.cron}` (KST)" if a.cron else "1회성"
                state = "" if a.enabled else " ⏸️꺼짐"
                lines.append(f"#{a.id} · <#{a.channel_id}> · {when}{state} · {a.message[:40]}")
            await ctx.respond("\n".join(lines)[:1900], ephemeral=True)

        @self.admin_command(name="announce-remove", description="예약 공지 삭제 (id).")
        async def announce_remove(ctx, ann_id: int):
            if self._announcements is None:
                await ctx.respond("예약 공지 저장소가 없어.", ephemeral=True)
                return
            ok = self._announcements.remove(int(ann_id), self._ctx_guild(ctx))
            await ctx.respond(f"{'🗑️ 삭제됨' if ok else '해당 id 없음'}: #{int(ann_id)}", ephemeral=True)

        @self.admin_command(name="announce-toggle", description="예약 공지 켜기/끄기 (id, enabled).")
        async def announce_toggle(ctx, ann_id: int, enabled: bool):
            if self._announcements is None:
                await ctx.respond("예약 공지 저장소가 없어.", ephemeral=True)
                return
            ok = self._announcements.set_enabled(int(ann_id), self._ctx_guild(ctx), enabled)
            await ctx.respond(
                f"{'✅' if ok else '해당 id 없음'}: #{int(ann_id)} {'켬' if enabled else '끔'}", ephemeral=True
            )

        @self.admin_command(
            name="event-add",
            description="행사 일정 카운트다운 공지 (D-30/14/7/3/1/DDAY 자동). 시각 KST.",
        )
        async def event_add(
            ctx,
            channel: discord.Option(discord.TextChannel, "공지 채널"),
            title: str,
            at: str,
            leads: str = "",
            note: str = "",
            mention: str = "",
        ):
            if self._events is None:
                await ctx.respond("행사 공지 저장소가 없어.", ephemeral=True)
                return
            import time as _t
            from datetime import datetime

            from ..service.announcements import EVENT_DEFAULT_LEADS, KST

            try:
                event_at = (
                    datetime.strptime(at.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=KST).timestamp()
                )
            except ValueError:
                await ctx.respond("시각 형식이 틀려. 예: 2026-07-15 19:00 (KST)", ephemeral=True)
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
                await ctx.respond("리드데이 형식이 틀려. 예: 30,14,7,3 (일 단위, 0=당일)", ephemeral=True)
                return
            eid = self._events.add(
                guild_id=self._ctx_guild(ctx),
                channel_id=channel.id,
                title=title.strip(),
                event_at=event_at,
                lead_days=lead_days,
                message=note.strip() or None,
                mention=mention.strip() or None,
                created_by=self._actor(ctx)[1],
            )
            now = _t.time()
            upcoming = [d for d in lead_days if event_at - d * 86400 > now]
            up_tags = "/".join("D-DAY" if d == 0 else f"D-{d}" for d in upcoming)
            head = f"✅ 행사 #{eid} 등록: {channel.mention} · **{title.strip()}** · {at.strip()} (KST)\n"
            if event_at + 3600 < now:
                plan = "⚠️ 이미 지난 행사라 공지는 나가지 않아 (기록만 됨)."
            elif any(event_at - d * 86400 <= now for d in lead_days):
                cur = max(0, round((event_at - now) / 86400))
                cur_tag = "D-DAY" if cur == 0 else f"D-{cur}"
                plan = f"지금 바로 **{cur_tag}** 공지 발송" + (f" → 이후 {up_tags}" if up_tags else "")
            else:
                plan = f"예정 공지: {up_tags}"
            await ctx.respond(head + plan, ephemeral=True)

        @self.admin_command(name="event-list", description="이 서버의 행사 일정 목록.")
        async def event_list(ctx):
            if self._events is None:
                await ctx.respond("행사 공지 저장소가 없어.", ephemeral=True)
                return
            from datetime import datetime

            from ..service.announcements import KST

            items = self._events.list_for_guild(self._ctx_guild(ctx))
            if not items:
                await ctx.respond("등록된 행사가 없어.", ephemeral=True)
                return
            lines = []
            for e in items:
                when = datetime.fromtimestamp(e.event_at, KST).strftime("%Y-%m-%d %H:%M")
                remaining = [d for d in e.lead_days if d not in e.fired_leads]
                tags = "/".join("DDAY" if d == 0 else f"D-{d}" for d in remaining) or "완료"
                state = "" if e.enabled else " ⏸️"
                lines.append(f"#{e.id} · <#{e.channel_id}> · {e.title} · {when}{state} · 남은: {tags}")
            await ctx.respond("\n".join(lines)[:1900], ephemeral=True)

        @self.admin_command(name="event-remove", description="행사 일정 삭제 (id).")
        async def event_remove(ctx, event_id: int):
            if self._events is None:
                await ctx.respond("행사 공지 저장소가 없어.", ephemeral=True)
                return
            ok = self._events.remove(int(event_id), self._ctx_guild(ctx))
            await ctx.respond(f"{'🗑️ 삭제됨' if ok else '해당 id 없음'}: #{int(event_id)}", ephemeral=True)

        @self.admin_command(name="event-toggle", description="행사 공지 켜기/끄기 (id, enabled).")
        async def event_toggle(ctx, event_id: int, enabled: bool):
            if self._events is None:
                await ctx.respond("행사 공지 저장소가 없어.", ephemeral=True)
                return
            ok = self._events.set_enabled(int(event_id), self._ctx_guild(ctx), enabled)
            await ctx.respond(
                f"{'✅' if ok else '해당 id 없음'}: #{int(event_id)} {'켬' if enabled else '끔'}", ephemeral=True
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
        """레벨 보상 역할 id 집합 — cleanup 이 보상 역할(멤버 0명이어도)을 고아로 삭제하지 않도록 보호."""
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
                        f"위험 작업이야: {res.detail}\n'확인 실행' 버튼을 누르거나 confirm:true 옵션으로 다시 실행해줘.",
                        view=view,
                        ephemeral=True,
                    )
                    return
            await ctx.respond(res.detail, ephemeral=True)
        except Exception:
            log.exception("admin command failed")
            await ctx.respond(
                "명령 처리 중 오류가 났어. 입력값(채널/역할 ID 등)을 확인하고 다시 시도해줘.",
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
        self._authorized = authorized  # NL 공개 메시지: 버튼 클릭자도 권한 재확인 (필수)

    async def _do_confirm(self, interaction) -> None:
        if self._authorized is not None and not self._authorized(interaction):
            await interaction.response.send_message(
                "⛔ 너는 이 작업을 확인할 권한이 없어 (운영진/주인만).", ephemeral=True
            )
            return
        await interaction.response.defer()  # ack the button click before the (possibly bulk) op
        try:
            res = await self._factory(self._token)
            msg = res.detail
        except Exception:
            log.exception("admin confirm failed")
            msg = "명령 처리 중 오류가 났어. 다시 시도해줘."
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(content=msg, view=self)
        self.stop()

    @discord.ui.button(label="확인 실행", style=discord.ButtonStyle.danger)
    async def confirm(self, button, interaction) -> None:
        await self._do_confirm(interaction)
