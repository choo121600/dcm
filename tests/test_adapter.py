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


class _FakeMessage:
    def __init__(self, channel, author, content, mentions, attachments=None, guild=None) -> None:
        self.channel = channel
        self.author = author
        self.content = content
        self.mentions = mentions
        self.clean_content = content
        self.attachments = attachments or []
        self.guild = guild


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
