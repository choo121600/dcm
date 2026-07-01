"""Adapter regression tests for the discord.Client -> discord.Bot migration (ralplan S1).

Run: .venv/bin/python -m pytest tests/test_adapter.py -q

Covers the asymmetric migration risk flagged by the architect (HIGH-1):
  * the NEW half  - application/slash command registration + sync wiring is intact on the Bot;
  * the inherited half - the @mention path still dispatches to the registered handler.

No live Discord connection: the real ``discord.Bot`` is used for registration, and lightweight
fakes drive the mention dispatch path (``on_message`` only reads ``self._client.user``).

The ``loop`` autouse fixture keeps a current event loop set on the main thread, because
``discord.Bot()`` construction calls ``asyncio.get_event_loop()`` under pycord on Python 3.12.
"""
import asyncio

import discord
import pytest

from dcm.platform.base import BufferedMessage, IncomingMessage
from dcm.platform.pycord_adapter import PycordAdapter, split_for_discord


@pytest.fixture(autouse=True)
def loop():
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    asyncio.set_event_loop(None)
    lp.close()


def _adapter() -> PycordAdapter:
    return PycordAdapter(
        token="test-token", bot_name="지우", buffer_size=5, cooldown_seconds=0.0, guild_id=123
    )


def test_adapter_client_is_bot_with_slash_registration() -> None:
    """Migration target is discord.Bot and application-command registration/sync wiring is intact."""
    adapter = _adapter()
    bot = adapter._client
    assert isinstance(bot, discord.Bot), type(bot)
    # discord.Bot retains the inherited Client surface the mention path relies on.
    for attr in ("event", "get_channel", "start", "user"):
        assert hasattr(bot, attr), attr

    # The NEW half: a slash command registers and queues for guild-scoped sync.
    @bot.slash_command(name="whoami_probe", description="probe", guild_ids=[123])
    async def _probe(ctx):  # pragma: no cover - never invoked here
        ...

    pending = [c.name for c in bot.pending_application_commands]
    assert "whoami_probe" in pending, pending


class _FakeUser:
    def __init__(self, uid: int, *, bot: bool = False, name: str = "u") -> None:
        self.id = uid
        self.bot = bot
        self.display_name = name


class _FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


_UNSET = object()


class _FakeGuild:
    def __init__(self, gid: int = 4242, owner_id: int = 0) -> None:
        self.id = gid
        self.owner_id = owner_id


class _FakeMessage:
    def __init__(self, channel, author, content, mentions, attachments=None, guild=_UNSET) -> None:
        self.channel = channel
        self.author = author
        self.content = content
        self.mentions = mentions
        self.clean_content = content
        self.attachments = attachments or []
        self.guild = _FakeGuild() if guild is _UNSET else guild


class _FakeChannel:
    def __init__(self, history_msgs) -> None:
        self.id = 4242
        self._history = history_msgs
        self.sent: list[str] = []

    def typing(self):
        return _FakeTyping()

    def history(self, limit):
        msgs = self._history[:limit]

        async def gen():
            for m in msgs:
                yield m

        return gen()

    async def send(self, text, **kw):
        self.sent.append(text)


def _swap_in_fake_user(adapter: PycordAdapter, bot_user: _FakeUser) -> None:
    # on_message reads only self._client.user; a stand-in avoids a live gateway connection.
    import types

    adapter._client = types.SimpleNamespace(user=bot_user)


def test_mention_dispatches_to_handler(loop) -> None:
    """The inherited @mention path still reaches the handler and sends the reply (mention stripped)."""
    adapter = _adapter()
    bot_user = _FakeUser(999, bot=True, name="지우")
    _swap_in_fake_user(adapter, bot_user)

    seen: dict = {}

    async def handler(incoming: IncomingMessage, buffer: list[BufferedMessage]):
        seen["incoming"] = incoming
        seen["buffer"] = buffer
        return "안녕 choo"

    adapter.on_mention(handler)

    author = _FakeUser(1, bot=False, name="choo")
    prior = _FakeMessage(None, _FakeUser(2, name="dora"), "hi", [])
    channel = _FakeChannel([prior])
    msg = _FakeMessage(channel, author, f"<@{bot_user.id}> 안녕", [bot_user])

    loop.run_until_complete(adapter.on_message(msg))

    assert "incoming" in seen, "handler was not dispatched on a mention"
    assert seen["incoming"].author_name == "choo"
    assert seen["incoming"].content == "안녕"  # mention token stripped
    assert seen["incoming"].channel_id == "4242"
    assert [b.author_name for b in seen["buffer"]] == ["dora"]
    assert channel.sent == ["안녕 choo"], channel.sent


def test_non_mention_is_ignored(loop) -> None:
    """A message that does not mention the bot must not dispatch."""
    adapter = _adapter()
    bot_user = _FakeUser(999, bot=True, name="지우")
    _swap_in_fake_user(adapter, bot_user)

    calls = {"n": 0}

    async def handler(incoming, buffer):
        calls["n"] += 1
        return None

    adapter.on_mention(handler)
    channel = _FakeChannel([])
    msg = _FakeMessage(channel, _FakeUser(1, name="choo"), "no mention here", [])
    loop.run_until_complete(adapter.on_message(msg))
    assert calls["n"] == 0
    assert channel.sent == []


def test_bot_author_is_ignored(loop) -> None:
    """Other bots (and the bot itself) never trigger a dispatch."""
    adapter = _adapter()
    bot_user = _FakeUser(999, bot=True, name="지우")
    _swap_in_fake_user(adapter, bot_user)
    calls = {"n": 0}

    async def handler(incoming, buffer):
        calls["n"] += 1
        return "x"

    adapter.on_mention(handler)
    channel = _FakeChannel([])
    other_bot = _FakeUser(7, bot=True, name="otherbot")
    msg = _FakeMessage(channel, other_bot, f"<@{bot_user.id}> hi", [bot_user])
    loop.run_until_complete(adapter.on_message(msg))
    assert calls["n"] == 0


def test_split_for_discord_respects_limit() -> None:
    chunks = split_for_discord("a" * 4500, limit=2000)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == "a" * 4500


def test_name_call_dispatches(loop) -> None:
    """Calling the bot by name at the start ("지우야 …") dispatches without an @mention."""
    adapter = _adapter()
    bot_user = _FakeUser(999, bot=True, name="지우")
    _swap_in_fake_user(adapter, bot_user)

    seen: dict = {}

    async def handler(incoming, buffer):
        seen["incoming"] = incoming
        return "왔어?"

    adapter.on_mention(handler)
    channel = _FakeChannel([])
    msg = _FakeMessage(channel, _FakeUser(1, name="choo"), "지우야 나 왔다", [])
    loop.run_until_complete(adapter.on_message(msg))

    assert "incoming" in seen, "name call did not dispatch"
    assert seen["incoming"].content == "지우야 나 왔다"
    assert channel.sent == ["왔어?"], channel.sent


def test_name_call_vocative_anywhere_dispatches(loop) -> None:
    """A vocative call elsewhere in the message ("안녕 지우야") also dispatches."""
    adapter = _adapter()
    bot_user = _FakeUser(999, bot=True, name="지우")
    _swap_in_fake_user(adapter, bot_user)
    calls = {"n": 0}

    async def handler(incoming, buffer):
        calls["n"] += 1
        return None

    adapter.on_mention(handler)
    channel = _FakeChannel([])
    msg = _FakeMessage(channel, _FakeUser(1, name="choo"), "안녕 지우야", [])
    loop.run_until_complete(adapter.on_message(msg))
    assert calls["n"] == 1


def test_third_person_name_is_ignored(loop) -> None:
    """Third-person mentions of the name ("지우가 …") must NOT dispatch (spam guard)."""
    adapter = _adapter()
    bot_user = _FakeUser(999, bot=True, name="지우")
    _swap_in_fake_user(adapter, bot_user)
    calls = {"n": 0}

    async def handler(incoming, buffer):
        calls["n"] += 1
        return None

    adapter.on_mention(handler)
    channel = _FakeChannel([])
    for text in ("지우가 아까 그랬어", "나는 지우 좋아해", "어제 지우랑 놀았어"):
        msg = _FakeMessage(channel, _FakeUser(1, name="choo"), text, [])
        loop.run_until_complete(adapter.on_message(msg))
    assert calls["n"] == 0, "third-person name mention should not trigger"


def test_reply_to_bot_dispatches(loop) -> None:
    """Replying to one of the bot's own messages dispatches without a name or @mention."""
    import types

    adapter = _adapter()
    bot_user = _FakeUser(999, bot=True, name="지우")
    _swap_in_fake_user(adapter, bot_user)
    calls = {"n": 0}

    async def handler(incoming, buffer):
        calls["n"] += 1
        return None

    adapter.on_mention(handler)
    channel = _FakeChannel([])
    msg = _FakeMessage(channel, _FakeUser(1, name="choo"), "그건 왜?", [])
    msg.reference = types.SimpleNamespace(resolved=types.SimpleNamespace(author=bot_user))
    loop.run_until_complete(adapter.on_message(msg))
    assert calls["n"] == 1


class _FakeAttachment:
    def __init__(self, filename, content, size=None):
        self.filename = filename
        self._data = content.encode("utf-8") if isinstance(content, str) else content
        self.size = size if size is not None else len(self._data)

    async def read(self):
        return self._data


class _FakeTemplateService:
    def __init__(self, result):
        self.calls = []
        self._result = result

    async def apply_template(self, *, guild_id, actor_name, actor_id, template_text=None, confirm_token=None):
        self.calls.append({"text": template_text, "token": confirm_token, "actor_id": actor_id})
        return self._result


def _dry_result():
    from dcm.service.guild_admin import OpResult

    return OpResult(
        False, "📋 템플릿 미리보기\n• 역할: 운영진", needs_confirmation=True, confirmation_token="tok-1"
    )


_TPL = "categories:\n  - name: 새카테고리\n    channels:\n      - name: 일반\n"


def test_nl_template_owner_previews_and_calls_service(loop):
    import types

    adapter = _adapter()
    svc = _FakeTemplateService(_dry_result())
    adapter._service = svc
    _swap_in_fake_user(adapter, _FakeUser(999, bot=True, name="지우"))
    author = _FakeUser(42, name="boss")
    guild = types.SimpleNamespace(id=123, owner_id=42)  # author == 길드 주인
    att = _FakeAttachment("server.yaml", _TPL)
    channel = _FakeChannel([])
    msg = _FakeMessage(channel, author, "지우야 이대로 세팅해줘", [], attachments=[att], guild=guild)
    loop.run_until_complete(adapter.on_message(msg))
    assert len(svc.calls) == 1, "owner의 템플릿 적용이 서비스로 가지 않음"
    assert svc.calls[0]["token"] is None  # 드라이런(확인 전)
    assert any(("미리보기" in s) or ("적용하려면" in s) for s in channel.sent), channel.sent


def test_nl_template_non_admin_refused(loop):
    import types

    adapter = _adapter()
    svc = _FakeTemplateService(_dry_result())
    adapter._service = svc
    _swap_in_fake_user(adapter, _FakeUser(999, bot=True, name="지우"))
    author = _FakeUser(7, name="rando")
    guild = types.SimpleNamespace(id=123, owner_id=42)  # author != owner, admin_role_id=0 → 비관리자
    att = _FakeAttachment("server.yaml", _TPL)
    channel = _FakeChannel([])
    msg = _FakeMessage(channel, author, "지우야 이대로 세팅해줘", [], attachments=[att], guild=guild)
    loop.run_until_complete(adapter.on_message(msg))
    assert svc.calls == [], "비관리자가 템플릿을 적용함"
    assert any(("운영진" in s) or ("주인" in s) for s in channel.sent), channel.sent


def test_nl_template_without_setup_intent_falls_to_persona(loop):
    import types

    adapter = _adapter()
    svc = _FakeTemplateService(_dry_result())
    adapter._service = svc
    _swap_in_fake_user(adapter, _FakeUser(999, bot=True, name="지우"))
    seen = {"n": 0}

    async def handler(incoming, buffer):
        seen["n"] += 1
        return None

    adapter.on_mention(handler)
    author = _FakeUser(42, name="boss")
    guild = types.SimpleNamespace(id=123, owner_id=42)
    att = _FakeAttachment("server.yaml", _TPL)
    channel = _FakeChannel([])
    # 셋업 의도 키워드 없음 → 템플릿 경로 미발동, 페르소나로 폴백
    msg = _FakeMessage(channel, author, "지우야 이 파일 좀 봐줘", [], attachments=[att], guild=guild)
    loop.run_until_complete(adapter.on_message(msg))
    assert svc.calls == [], "의도 키워드 없는데 템플릿이 발동함"
    assert seen["n"] == 1, "페르소나 핸들러로 가지 않음"


def test_confirm_view_rejects_unauthorized_clicker(loop):
    called = {"n": 0}

    async def scenario():
        from dcm.platform.pycord_adapter import _ConfirmView

        async def factory(token):
            called["n"] += 1
            return None

        class _Resp:
            def __init__(self):
                self.msgs = []

            async def send_message(self, text, **kw):
                self.msgs.append(text)

            async def defer(self):
                pass

        class _Inter:
            def __init__(self):
                self.response = _Resp()

        view = _ConfirmView(factory, "tok-1", authorized=lambda i: False)
        inter = _Inter()
        await view._do_confirm(inter)
        return inter

    inter = loop.run_until_complete(scenario())
    assert called["n"] == 0, "권한 없는 클릭인데 적용됨"
    assert inter.response.msgs and "권한" in inter.response.msgs[-1]


def test_export_server_yaml_from_guild_roundtrips():
    import discord
    from dcm.service import guild_admin as ga
    from dcm.service.template import parse_template

    class _NS:  # identity-hashable (SimpleNamespace는 __eq__ 정의로 unhashable이라 dict 키/set 불가)
        def __init__(self, **kw):
            self.__dict__.update(kw)

    adapter = _adapter()
    everyone = _NS(name="@everyone", managed=False, permissions=_NS(value=0))
    admin_role = _NS(name="운영진", managed=False, permissions=_NS(value=ga.MANAGE_CHANNELS | ga.MANAGE_ROLES))
    member_role = _NS(name="멤버", managed=False, permissions=_NS(value=0))
    bot_role = _NS(name="dcm-bot", managed=True, permissions=_NS(value=ga.ADMINISTRATOR))
    cat = _NS(
        name="2026-summer",
        overwrites={everyone: _NS(view_channel=False), admin_role: _NS(view_channel=True)},
        channels=[],
    )
    notice = _NS(name="공지", type=discord.ChannelType.text, category=cat)
    voice = _NS(name="회의", type=discord.ChannelType.voice, category=cat)
    cat.channels = [notice, voice]
    orphan = _NS(name="lobby", type=discord.ChannelType.text, category=None)
    guild = _NS(
        default_role=everyone,
        roles=[everyone, member_role, admin_role, bot_role],
        categories=[cat],
        channels=[cat, notice, voice, orphan],
    )

    text = adapter._export_server_yaml(guild)
    assert "lobby" in text  # 무카테고리 최상위 채널은 주석으로 안내
    t = parse_template(text)
    assert [r.name for r in t.roles] == ["운영진", "멤버"]  # @everyone + managed(bot) 제외, 높은역할 먼저
    assert set(t.roles[0].permission_names) == {"manage_channels", "manage_roles"}
    c0 = t.categories[0]
    assert c0.name == "2026-summer" and c0.private is True
    assert "운영진" in c0.visible_to
    assert {(ch.name, ch.kind) for ch in c0.channels} == {("공지", "text"), ("회의", "voice")}


def test_nl_export_owner_sends_file(loop):
    import types

    adapter = _adapter()
    _swap_in_fake_user(adapter, _FakeUser(999, bot=True, name="지우"))
    author = _FakeUser(42, name="boss")
    everyone = types.SimpleNamespace(name="@everyone", managed=False, permissions=types.SimpleNamespace(value=0))
    guild = types.SimpleNamespace(id=123, owner_id=42, default_role=everyone, roles=[everyone], categories=[], channels=[])
    channel = _FakeChannel([])
    msg = _FakeMessage(channel, author, "지우야 서버 구조 yaml로 뽑아줘", [], attachments=[], guild=guild)
    loop.run_until_complete(adapter.on_message(msg))
    assert any("현재 서버 구조" in s for s in channel.sent), channel.sent


def test_nl_export_non_admin_refused(loop):
    import types

    adapter = _adapter()
    _swap_in_fake_user(adapter, _FakeUser(999, bot=True, name="지우"))
    author = _FakeUser(7, name="rando")
    everyone = types.SimpleNamespace(name="@everyone", managed=False, permissions=types.SimpleNamespace(value=0))
    guild = types.SimpleNamespace(id=123, owner_id=42, default_role=everyone, roles=[everyone], categories=[], channels=[])
    channel = _FakeChannel([])
    msg = _FakeMessage(channel, author, "지우야 서버 구조 yaml로 뽑아줘", [], attachments=[], guild=guild)
    loop.run_until_complete(adapter.on_message(msg))
    assert any(("운영진" in s) or ("주인" in s) for s in channel.sent), channel.sent


def test_dm_is_ignored(loop):
    """DM(guild 없음)은 호명·핸들러와 무관하게 전면 무시된다 (P2 fail-closed)."""
    adapter = _adapter()
    _swap_in_fake_user(adapter, _FakeUser(999, bot=True, name="지우"))
    calls = {"n": 0}

    async def handler(incoming, buffer):
        calls["n"] += 1
        return "응"

    adapter.on_mention(handler)
    channel = _FakeChannel([])
    msg = _FakeMessage(channel, _FakeUser(1, name="choo"), "지우야 안녕", [], guild=None)
    loop.run_until_complete(adapter.on_message(msg))
    assert calls["n"] == 0, "DM이 핸들러로 전달됨"
    assert channel.sent == []


def test_guild_message_sets_guild_id(loop):
    """길드 메시지는 incoming.guild_id를 message.guild.id로 채운다."""
    adapter = _adapter()
    _swap_in_fake_user(adapter, _FakeUser(999, bot=True, name="지우"))
    seen = {}

    async def handler(incoming, buffer):
        seen["guild_id"] = incoming.guild_id
        return None

    adapter.on_mention(handler)
    channel = _FakeChannel([])
    msg = _FakeMessage(channel, _FakeUser(1, name="choo"), "지우야 안녕", [], guild=_FakeGuild(gid=55))
    loop.run_until_complete(adapter.on_message(msg))
    assert seen.get("guild_id") == 55

# ---------------------------------------------------------------------------
# P4: 글로벌 등록 + 설정 슬래시 커맨드 테스트
# ---------------------------------------------------------------------------

_SETTINGS_ADMIN_ROLE = 999


class _FakeSettings:
    """테스트용 GuildSettingsStore 더블. set_* 호출을 기록하고 get은 고정값 반환."""

    def __init__(self, admin_role_id=_SETTINGS_ADMIN_ROLE):
        self._admin_role_id = admin_role_id
        self.set_admin_role_calls: list = []
        self.set_welcome_channel_calls: list = []
        self.set_welcome_message_calls: list = []
        self.set_default_role_calls: list = []
        self._store: dict = {}

    def get(self, guild_id):
        import types as _t
        data = self._store.get(str(guild_id), {})
        return _t.SimpleNamespace(
            admin_role_id=data.get("admin_role_id", self._admin_role_id),
            welcome_channel_id=data.get("welcome_channel_id", 555),
            default_role_id=data.get("default_role_id", 777),
            welcome_message=data.get("welcome_message", "환영해!"),
        )

    def set_admin_role(self, guild_id, role_id):
        self.set_admin_role_calls.append((guild_id, role_id))
        self._store.setdefault(str(guild_id), {})["admin_role_id"] = role_id

    def set_welcome_channel(self, guild_id, channel_id):
        self.set_welcome_channel_calls.append((guild_id, channel_id))

    def set_welcome_message(self, guild_id, message):
        self.set_welcome_message_calls.append((guild_id, message))

    def set_default_role(self, guild_id, role_id):
        self.set_default_role_calls.append((guild_id, role_id))


class _SettingsAuthor:
    """관리자 역할(999) 보유 멤버 더블."""

    def __init__(self):
        import types as _t
        self.roles = [_t.SimpleNamespace(id=_SETTINGS_ADMIN_ROLE)]
        self.id = 42
        self.display_name = "choo"


class _SettingsCtx:
    """admin_command InvokerCheck를 통과하는 ctx 더블."""

    def __init__(self):
        self.author = _SettingsAuthor()
        self.guild_id = 123
        self.responses: list = []

    async def respond(self, text, **kw):
        self.responses.append((text, kw))


def _settings_adapter(settings=None):
    """guild_settings 주입 어댑터 (guild_id=123, admin_role_id는 settings에서 옴)."""
    from dcm.service.guild_admin import GuildAdminService

    st = settings if settings is not None else _FakeSettings()
    a = PycordAdapter(
        token="x",
        bot_name="지우",
        guild_id=123,
        admin_role_id=0,  # settings 스토어가 admin_role_id를 제공하므로 env 값은 0
        guild_settings=st,
    )
    a.register_admin_commands(GuildAdminService(a, a.pending))
    return a, st


def _find_cmd(adapter, name):
    cmds = list(adapter._client.pending_application_commands)
    return next((c for c in cmds if c.name == name), None)


# 1) 글로벌 등록 회귀 -------------------------------------------------------

def test_all_admin_commands_registered_globally(loop):
    """register_admin_commands 후 모든 커맨드의 guild_ids가 비어있거나 None(글로벌)이어야 한다."""
    a, _ = _settings_adapter()
    cmds = list(a._client.pending_application_commands)
    assert cmds, "커맨드가 하나도 등록되지 않음"
    for cmd in cmds:
        gids = getattr(cmd, "guild_ids", None)
        assert not gids, f"{cmd.name}: guild_ids={gids} (글로벌이어야 함)"


def test_global_registration_still_has_invokercheck(loop):
    """글로벌 등록 후에도 by-construction InvokerCheck가 여전히 동작한다."""
    a, _ = _settings_adapter()
    cmds = list(a._client.pending_application_commands)
    for cmd in cmds:
        assert getattr(cmd.callback, "__gjc_admin_guarded__", False), (
            f"{cmd.name}: InvokerCheck 래퍼 누락"
        )
        # 비관리자 ctx로 거부 확인
        ctx = _SettingsCtx()
        ctx.author.roles = []  # 역할 없음 → 거부
        loop.run_until_complete(cmd.callback(ctx))
        assert ctx.responses and "관리자" in ctx.responses[-1][0], (
            f"{cmd.name}: 비관리자를 거부하지 않음"
        )


# 2) set-admin-role 동작 -------------------------------------------------------

def test_set_admin_role_records_call(loop):
    """set-admin-role 호출 시 settings.set_admin_role(guild_id, role_id) 가 기록된다."""
    a, st = _settings_adapter()
    cmd = _find_cmd(a, "set-admin-role")
    assert cmd is not None, "set-admin-role 커맨드 미등록"
    ctx = _SettingsCtx()
    loop.run_until_complete(cmd.callback(ctx, role_id="888"))
    assert st.set_admin_role_calls == [(123, 888)], st.set_admin_role_calls
    assert ctx.responses, "응답 없음"
    text, kw = ctx.responses[-1]
    assert "888" in text
    assert kw.get("ephemeral") is True


def test_set_admin_role_bad_int_returns_error(loop):
    """set-admin-role에 정수가 아닌 role_id를 넘기면 ephemeral 에러를 반환한다."""
    a, st = _settings_adapter()
    cmd = _find_cmd(a, "set-admin-role")
    ctx = _SettingsCtx()
    loop.run_until_complete(cmd.callback(ctx, role_id="not-a-number"))
    assert st.set_admin_role_calls == [], "잘못된 입력인데도 set_admin_role 이 호출됨"
    assert ctx.responses
    text, kw = ctx.responses[-1]
    assert "정수" in text
    assert kw.get("ephemeral") is True


def test_set_admin_role_no_settings_returns_error(loop):
    """settings 미주입 어댑터에서 set-admin-role 호출 시 안내 메시지를 반환한다."""
    from dcm.service.guild_admin import GuildAdminService

    # settings 없이, admin_role_id로 authz 통과
    a = PycordAdapter(token="x", bot_name="지우", guild_id=123, admin_role_id=_SETTINGS_ADMIN_ROLE)
    a.register_admin_commands(GuildAdminService(a, a.pending))
    cmd = _find_cmd(a, "set-admin-role")
    ctx = _SettingsCtx()
    loop.run_until_complete(cmd.callback(ctx, role_id="888"))
    assert ctx.responses
    text, kw = ctx.responses[-1]
    assert "저장소" in text
    assert kw.get("ephemeral") is True


# 3) show-config 동작 -------------------------------------------------------

def test_show_config_displays_settings(loop):
    """show-config 는 settings.get 값을 한국어로 ephemeral 표시한다."""
    a, st = _settings_adapter()
    cmd = _find_cmd(a, "show-config")
    assert cmd is not None, "show-config 커맨드 미등록"
    ctx = _SettingsCtx()
    loop.run_until_complete(cmd.callback(ctx))
    assert ctx.responses, "응답 없음"
    text, kw = ctx.responses[-1]
    assert kw.get("ephemeral") is True
    # _FakeSettings.get(123) → admin_role_id=999, welcome_channel_id=555, default_role_id=777
    assert "999" in text, f"admin_role_id 미포함: {text!r}"
    assert "555" in text, f"welcome_channel_id 미포함: {text!r}"
    assert "777" in text, f"default_role_id 미포함: {text!r}"
    assert "환영해!" in text, f"welcome_message 미포함: {text!r}"


def test_show_config_unset_shows_미설정(loop):
    """설정값이 None/0인 경우 '미설정' 으로 표시한다."""
    import types as _t

    class _EmptySettings:
        def get(self, guild_id):
            return _t.SimpleNamespace(
                admin_role_id=_SETTINGS_ADMIN_ROLE,  # 인증 통과용 (999)
                welcome_channel_id=None,  # 미설정
                default_role_id=None,     # 미설정
                welcome_message=None,     # 미설정
            )

    from dcm.service.guild_admin import GuildAdminService

    a = PycordAdapter(
        token="x",
        bot_name="지우",
        guild_id=123,
        admin_role_id=_SETTINGS_ADMIN_ROLE,
        guild_settings=_EmptySettings(),
    )
    a.register_admin_commands(GuildAdminService(a, a.pending))
    cmd = _find_cmd(a, "show-config")
    ctx = _SettingsCtx()
    loop.run_until_complete(cmd.callback(ctx))
    text, kw = ctx.responses[-1]
    assert "미설정" in text, f"미설정 미포함: {text!r}"
    assert kw.get("ephemeral") is True


# ---------------------------------------------------------------------------
# P2 보강: NL 템플릿/익스포트 authz가 per-guild 설정(_is_admin chokepoint)을 사용
# ---------------------------------------------------------------------------


class TestNlAuthzPerGuild:
    """NL 경로(_handle_template_attachment/_handle_export)가 정적 env 역할이 아니라
    그 길드의 설정 관리역할 + manage_guild 폴백(_is_admin)을 통과해야 한다."""

    @staticmethod
    def _member(uid, role_ids=(), manage=False):
        import types

        m = types.SimpleNamespace(id=uid, display_name="u")
        m.roles = [types.SimpleNamespace(id=r) for r in role_ids]
        if manage:
            m.guild_permissions = types.SimpleNamespace(manage_guild=True, administrator=False)
        return m

    def test_is_admin_uses_per_guild_role(self):
        a, _ = _settings_adapter()  # 모든 길드 admin_role_id == 999
        ch = _FakeChannel([])
        ok = _FakeMessage(ch, self._member(1, role_ids=(999,)), "x", [], guild=_FakeGuild(gid=55))
        assert a._is_admin(ok) is True  # 비시드 길드에서도 그 길드 역할로 통과
        no = _FakeMessage(ch, self._member(2), "x", [], guild=_FakeGuild(gid=55))
        assert a._is_admin(no) is False  # 역할/주인/manage 모두 없음 → 거부

    def test_is_admin_owner_always_passes(self):
        a, _ = _settings_adapter()
        ch = _FakeChannel([])
        owner = _FakeMessage(ch, self._member(7), "x", [], guild=_FakeGuild(gid=55, owner_id=7))
        assert a._is_admin(owner) is True

    def test_manage_guild_fallback_only_when_role_unset(self):
        a0, _ = _settings_adapter(_FakeSettings(admin_role_id=0))  # 관리역할 미설정 길드
        ch = _FakeChannel([])
        mg = _FakeMessage(ch, self._member(3, manage=True), "x", [], guild=_FakeGuild(gid=55))
        assert a0._is_admin(mg) is True  # 미설정 → manage_guild 폴백 허용
        a1, _ = _settings_adapter()  # admin_role=999 설정 길드
        mg1 = _FakeMessage(ch, self._member(4, manage=True), "x", [], guild=_FakeGuild(gid=55))
        assert a1._is_admin(mg1) is False  # 설정됨 → 폴백 미트리거(좁은 권한)

    def test_export_denies_non_admin(self, loop):
        a, _ = _settings_adapter()
        ch = _FakeChannel([])
        msg = _FakeMessage(ch, self._member(9), "지우야 서버 구조 yaml로 뽑아줘", [], guild=_FakeGuild(gid=55))
        loop.run_until_complete(a._handle_export(msg))
        assert any("내보내기" in s for s in ch.sent)  # 비관리자 → 거부 메시지


# ---------------------------------------------------------------------------
# 예약 공지 슬래시 커맨드 배선
# ---------------------------------------------------------------------------


class TestAnnounceCommands:
    @staticmethod
    def _adapter(tmp_path):
        import types

        from dcm.service.announcements import AnnouncementStore
        from dcm.service.guild_admin import GuildAdminService

        store = AnnouncementStore(str(tmp_path / "ann.db"))
        a = PycordAdapter(
            token="x",
            bot_name="지우",
            guild_id=123,
            admin_role_id=0,
            guild_settings=_FakeSettings(),
            announcements=store,
        )
        a.register_admin_commands(GuildAdminService(a, a.pending))
        return a, store, types.SimpleNamespace(id=555, mention="<#555>")

    def test_announce_add_records_recurring(self, loop, tmp_path):
        a, store, ch = self._adapter(tmp_path)
        cmd = _find_cmd(a, "announce-add")
        loop.run_until_complete(cmd.callback(_SettingsCtx(), channel=ch, message="주간 회의", cron="0 9 * * 1"))
        items = store.list_for_guild(123)
        assert len(items) == 1 and items[0].cron == "0 9 * * 1" and items[0].channel_id == "555"
        store.close()

    def test_announce_add_bad_cron_reports_error(self, loop, tmp_path):
        a, store, ch = self._adapter(tmp_path)
        ctx = _SettingsCtx()
        loop.run_until_complete(_find_cmd(a, "announce-add").callback(ctx, channel=ch, message="m", cron="nope"))
        assert store.list_for_guild(123) == []  # 등록 안 됨
        assert any("cron" in r[0] for r in ctx.responses)
        store.close()

    def test_announce_add_once_parses_kst(self, loop, tmp_path):
        a, store, ch = self._adapter(tmp_path)
        loop.run_until_complete(
            _find_cmd(a, "announce-add-once").callback(_SettingsCtx(), channel=ch, message="이벤트", at="2026-07-15 10:00")
        )
        items = store.list_for_guild(123)
        assert len(items) == 1 and items[0].run_at is not None and items[0].cron is None
        store.close()

    def test_announce_list_and_remove(self, loop, tmp_path):
        a, store, ch = self._adapter(tmp_path)
        aid = store.add(guild_id=123, channel_id=555, message="m", cron="* * * * *")
        ctx = _SettingsCtx()
        loop.run_until_complete(_find_cmd(a, "announce-list").callback(ctx))
        assert any(f"#{aid}" in r[0] for r in ctx.responses)
        loop.run_until_complete(_find_cmd(a, "announce-remove").callback(_SettingsCtx(), ann_id=aid))
        assert store.list_for_guild(123) == []
        store.close()


class TestEventCommands:
    @staticmethod
    def _adapter(tmp_path):
        import types

        from dcm.service.announcements import EventStore
        from dcm.service.guild_admin import GuildAdminService

        store = EventStore(str(tmp_path / "evt.db"))
        a = PycordAdapter(
            token="x",
            bot_name="지우",
            guild_id=123,
            admin_role_id=0,
            guild_settings=_FakeSettings(),
            events=store,
        )
        a.register_admin_commands(GuildAdminService(a, a.pending))
        return a, store, types.SimpleNamespace(id=555, mention="<#555>")

    def test_event_add_default_countdown(self, loop, tmp_path):
        a, store, ch = self._adapter(tmp_path)
        ctx = _SettingsCtx()
        loop.run_until_complete(
            _find_cmd(a, "event-add").callback(ctx, channel=ch, title="여름 OT", at="2099-07-15 19:00")
        )
        items = store.list_for_guild(123)
        assert len(items) == 1
        assert items[0].title == "여름 OT" and items[0].channel_id == "555"
        assert items[0].lead_days == (30, 14, 7, 3, 1, 0)  # 기본 카운트다운
        store.close()

    def test_event_add_custom_leads_and_note(self, loop, tmp_path):
        a, store, ch = self._adapter(tmp_path)
        loop.run_until_complete(
            _find_cmd(a, "event-add").callback(
                _SettingsCtx(), channel=ch, title="총회", at="2099-08-01 20:00",
                leads="14,7,3", note="본관", mention="@everyone",
            )
        )
        e = store.list_for_guild(123)[0]
        assert e.lead_days == (14, 7, 3) and e.message == "본관" and e.mention == "@everyone"
        store.close()

    def test_event_add_bad_datetime_reports_error(self, loop, tmp_path):
        a, store, ch = self._adapter(tmp_path)
        ctx = _SettingsCtx()
        loop.run_until_complete(
            _find_cmd(a, "event-add").callback(ctx, channel=ch, title="x", at="nope")
        )
        assert store.list_for_guild(123) == []
        assert any("시각" in r[0] for r in ctx.responses)
        store.close()

    def test_event_add_bad_leads_reports_error(self, loop, tmp_path):
        a, store, ch = self._adapter(tmp_path)
        ctx = _SettingsCtx()
        loop.run_until_complete(
            _find_cmd(a, "event-add").callback(ctx, channel=ch, title="x", at="2099-08-01 20:00", leads="a,b")
        )
        assert store.list_for_guild(123) == []
        assert any("리드데이" in r[0] for r in ctx.responses)
        store.close()

    def test_event_list_and_remove(self, loop, tmp_path):
        a, store, ch = self._adapter(tmp_path)
        import time as _t

        eid = store.add(
            guild_id=123, channel_id=555, title="정기모임", event_at=_t.time() + 40 * 86400
        )
        ctx = _SettingsCtx()
        loop.run_until_complete(_find_cmd(a, "event-list").callback(ctx))
        assert any(f"#{eid}" in r[0] and "정기모임" in r[0] for r in ctx.responses)
        loop.run_until_complete(_find_cmd(a, "event-remove").callback(_SettingsCtx(), event_id=eid))
        assert store.list_for_guild(123) == []
        store.close()

    def test_event_toggle(self, loop, tmp_path):
        a, store, ch = self._adapter(tmp_path)
        import time as _t

        eid = store.add(guild_id=123, channel_id=555, title="x", event_at=_t.time() + 40 * 86400)
        loop.run_until_complete(
            _find_cmd(a, "event-toggle").callback(_SettingsCtx(), event_id=eid, enabled=False)
        )
        assert store.list_enabled() == []
        store.close()
