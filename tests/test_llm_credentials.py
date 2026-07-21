"""LLM credential base_url / failover tests (ARCHITECTURE.md §9.1).

Covers:
  (a) Credential defaults base_url to None
  (b) parse_credentials yields per-key credentials with no base_url
  (c) LLMClient forwards each credential's base_url to its AsyncAnthropic client
  (d) a credential without base_url keeps Anthropic's default host
  (e) complete() fails over to the fallback (proxy) credential after a real key errors
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import anthropic

from dcm.llm import Credential, LLMClient, build_credentials, parse_credentials


def test_credential_base_url_defaults_none():
    assert Credential(api_key="k", label="key1").base_url is None


def test_parse_credentials_have_no_base_url():
    creds = parse_credentials("a, b")
    assert [c.label for c in creds] == ["key1", "key2"]
    assert all(c.base_url is None for c in creds)


def test_client_forwards_base_url_per_credential(monkeypatch):
    # Hermetic: ignore any ANTHROPIC_BASE_URL the dev/CI host may export.
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    llm = LLMClient(
        [
            Credential(api_key="k", label="key1"),
            Credential(api_key="k", label="proxy", base_url="http://127.0.0.1:8787"),
        ],
        model="m",
        max_tokens=10,
    )
    # The override endpoint reaches the underlying client...
    assert str(llm._clients["proxy"].base_url).startswith("http://127.0.0.1:8787")
    # ...while a plain credential keeps Anthropic's default host.
    assert "api.anthropic.com" in str(llm._clients["key1"].base_url)


class _Boom(anthropic.APIError):
    """Minimal APIError — base __init__ (needs an httpx request) is intentionally skipped."""

    def __init__(self) -> None:
        pass


def _text_client(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=resp)
    return client


def _failing_client() -> MagicMock:
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=_Boom())
    return client


def test_complete_fails_over_to_proxy_after_real_key_errors():
    llm = LLMClient.__new__(LLMClient)
    llm._creds = [
        Credential(api_key="k", label="key1"),
        Credential(api_key="k", label="proxy", base_url="http://127.0.0.1:8787"),
    ]
    llm._model = "m"
    llm._max_tokens = 10
    key1 = _failing_client()
    proxy = _text_client("from proxy")
    llm._clients = {"key1": key1, "proxy": proxy}

    text, web_used = asyncio.run(
        llm.complete(system="s", messages=[{"role": "user", "content": "hi"}])
    )

    # Primary key was attempted and errored; the proxy credential answered.
    assert text == "from proxy"
    assert web_used is False
    key1.messages.create.assert_awaited_once()
    proxy.messages.create.assert_awaited_once()


class _ConnBoom(anthropic.APIConnectionError):
    """Minimal APIConnectionError — base __init__ (needs a request) is intentionally skipped."""

    def __init__(self) -> None:
        pass


def _conn_failing_client() -> MagicMock:
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=_ConnBoom())
    return client


# --- build_credentials: failover ordering (ARCHITECTURE.md §9.1) ---


def test_build_credentials_keys_only():
    creds = build_credentials("a, b")
    assert [c.label for c in creds] == ["key1", "key2"]
    assert all(c.base_url is None for c in creds)


def test_build_credentials_appends_proxy_as_last_resort():
    creds = build_credentials("a", fallback_base_url="http://127.0.0.1:8787")
    assert [c.label for c in creds] == ["key1", "proxy"]  # proxy tried last
    proxy = creds[-1]
    assert proxy.base_url == "http://127.0.0.1:8787"
    # Last-resort mode leaves the fast-failover knobs off → behavior unchanged.
    assert proxy.connect_timeout is None
    assert proxy.breaker_cooldown == 0.0


def test_build_credentials_prefers_proxy_first():
    creds = build_credentials(
        "a",
        fallback_base_url="http://127.0.0.1:8787",
        fallback_api_key="tok",
        prefer_proxy=True,
        proxy_connect_timeout=1.5,
        proxy_breaker_cooldown=30.0,
    )
    assert [c.label for c in creds] == ["proxy", "key1"]  # proxy tried first
    proxy = creds[0]
    assert proxy.api_key == "tok"
    assert proxy.connect_timeout == 1.5
    assert proxy.breaker_cooldown == 30.0


def test_build_credentials_proxy_reuses_primary_key_when_no_token():
    creds = build_credentials("realkey", fallback_base_url="http://x:1", prefer_proxy=True)
    proxy = creds[0]
    assert proxy.label == "proxy"
    assert proxy.api_key == "realkey"  # empty fallback_api_key → reuse the primary key


def test_connect_timeout_forwarded_only_when_set(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: calls.append(kw) or MagicMock())
    LLMClient(
        [
            Credential(api_key="k", label="key1"),
            Credential(api_key="k", label="proxy", base_url="http://x:1", connect_timeout=1.5),
        ],
        model="m",
        max_tokens=10,
    )
    key1_kw, proxy_kw = calls
    assert "timeout" not in key1_kw  # plain credential → keep the SDK default timeout
    assert proxy_kw["timeout"].connect == 1.5  # proxy → short connect bound for fast failover


def test_circuit_breaker_deprioritizes_proxy_after_connection_error():
    llm = LLMClient.__new__(LLMClient)
    llm._creds = [
        Credential(api_key="k", label="proxy", base_url="http://127.0.0.1:8787", breaker_cooldown=30.0),
        Credential(api_key="k", label="key1"),
    ]
    llm._model = "m"
    llm._max_tokens = 10
    proxy = _conn_failing_client()
    key1 = _text_client("from key")
    llm._clients = {"proxy": proxy, "key1": key1}

    def run() -> tuple[str, bool]:
        return asyncio.run(llm.complete(system="s", messages=[{"role": "user", "content": "hi"}]))

    # 1st call: proxy is tried first, connection-fails (opens its circuit), the key answers.
    text, _ = run()
    assert text == "from key"
    assert proxy.messages.create.await_count == 1
    assert key1.messages.create.await_count == 1

    # 2nd call within cooldown: the offline proxy sinks to the back, so the key answers WITHOUT
    # re-probing it — no repeated connect-timeout penalty on every message.
    text, _ = run()
    assert text == "from key"
    assert proxy.messages.create.await_count == 1  # not retried while the circuit is open
    assert key1.messages.create.await_count == 2
