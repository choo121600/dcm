from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import anthropic
import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Credential:
    """One LLM credential. The key value is never logged — only `label` (ARCHITECTURE.md §9.1)."""

    api_key: str
    label: str
    org: str | None = None
    base_url: str | None = None  # Messages API base override; None → default host (failover/proxy, §9.1)
    # Fast-failover knobs for a local/proxy credential (ARCHITECTURE.md §9.1):
    connect_timeout: float | None = None  # short TCP-connect bound so an unreachable host fails fast
    breaker_cooldown: float = 0.0  # >0 → after a connection failure, deprioritize this credential for N s


def parse_credentials(raw: str) -> list[Credential]:
    """Parse a comma-separated key string into the credential list (one key = pool of 1)."""
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return [Credential(api_key=k, label=f"key{i + 1}") for i, k in enumerate(keys)]


def build_credentials(
    api_keys: str,
    *,
    fallback_base_url: str = "",
    fallback_api_key: str = "",
    prefer_proxy: bool = False,
    proxy_connect_timeout: float | None = None,
    proxy_breaker_cooldown: float = 0.0,
) -> list[Credential]:
    """Assemble the failover-ordered credential list (ARCHITECTURE.md §9.1).

    The real key(s) in `api_keys` (comma-separated → pool) are the baseline. An optional proxy
    endpoint (`fallback_base_url`) is either appended as a last resort (default) or, when
    `prefer_proxy` is set, inserted at the FRONT so a reachable local proxy is used first and the
    API key becomes the fallback for when that proxy host is offline. The fast-failover knobs
    (short connect timeout + circuit breaker) apply to the proxy only in prefer mode, so
    last-resort behavior is unchanged.
    """
    creds = parse_credentials(api_keys)
    if fallback_base_url:
        primary_key = creds[0].api_key if creds else ""
        proxy = Credential(
            api_key=fallback_api_key or primary_key,
            label="proxy",
            base_url=fallback_base_url,
            connect_timeout=proxy_connect_timeout if prefer_proxy else None,
            breaker_cooldown=proxy_breaker_cooldown if prefer_proxy else 0.0,
        )
        creds.insert(0, proxy) if prefer_proxy else creds.append(proxy)
    return creds


class LLMClient:
    """Anthropic calls behind a credential list + selection strategy (ARCHITECTURE.md §9.1).

    M1 uses a single key, so `complete()` simply tries credentials in order. Round-robin /
    least-used strategies can slot in here later without touching callers.
    """

    def __init__(self, creds: list[Credential], model: str, max_tokens: int) -> None:
        if not creds:
            raise ValueError("at least one credential is required")
        self._creds = creds
        self._model = model
        self._max_tokens = max_tokens
        self._clients = {c.label: self._build_client(c) for c in creds}
        # Circuit breaker: credential label → monotonic deadline until which it is deprioritized
        # after a connection failure (§9.1); lazily (re)created by _breaker() for __new__ doubles.
        self._open_until: dict[str, float] = {}

    @staticmethod
    def _build_client(cred: Credential) -> anthropic.AsyncAnthropic:
        kwargs: dict = {"api_key": cred.api_key, "base_url": cred.base_url}
        if cred.connect_timeout is not None:
            # Bound only the TCP connect; leave read/write unbounded so long generations aren't cut.
            kwargs["timeout"] = httpx.Timeout(None, connect=cred.connect_timeout)
        return anthropic.AsyncAnthropic(**kwargs)

    def _breaker(self) -> dict[str, float]:
        # Lazy so hand-built test doubles (LLMClient.__new__) need no extra wiring.
        breaker = self.__dict__.get("_open_until")
        if breaker is None:
            breaker = self.__dict__["_open_until"] = {}
        return breaker

    def _ordered(self) -> list[Credential]:
        """Try-order for one call: credentials whose circuit is open sink to the back but are
        still attempted last, so a transient outage never leaves a credential untried (§9.1)."""
        breaker = self._breaker()
        now = time.monotonic()
        healthy: list[Credential] = []
        cooling: list[Credential] = []
        for cred in self._creds:
            (cooling if now < breaker.get(cred.label, 0.0) else healthy).append(cred)
        return healthy + cooling

    def _record_failure(self, cred: Credential, exc: Exception) -> None:
        """Open the circuit only for a genuine connectivity failure (host asleep/unreachable) on a
        breaker-enabled credential — never for a 4xx/5xx from a live endpoint (§9.1)."""
        if cred.breaker_cooldown > 0.0 and isinstance(exc, anthropic.APIConnectionError):
            self._breaker()[cred.label] = time.monotonic() + cred.breaker_cooldown

    async def complete(
        self,
        system: str,
        messages: list[dict],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        web_search: bool = False,
    ) -> tuple[str, bool]:
        """Request a text completion. When web_search=True, attach Anthropic's built-in web_search tool.

        Returns: (text, web_used) — the key is never written to logs (ARCHITECTURE.md §14.1).
        """
        last_error: Exception | None = None
        for cred in self._ordered():  # circuit-aware failover order (§9.1)
            client = self._clients[cred.label]
            try:
                base_kwargs: dict = dict(
                    model=model or self._model,
                    max_tokens=max_tokens or self._max_tokens,
                    system=system,
                    messages=messages,
                )
                if web_search:
                    try:
                        resp = await client.messages.create(
                            **base_kwargs,
                            tools=[{"type": "web_search_20250305", "name": "web_search"}],
                        )
                        text = "".join(b.text for b in resp.content if b.type == "text")
                        # web_used=True only if the response actually contains a web_search block
                        web_used = any(
                            getattr(b, "type", "") in {"server_tool_use", "web_search_tool_result"}
                            for b in resp.content
                        )
                        return text, web_used
                    except anthropic.BadRequestError:
                        # built-in web_search unavailable (account/model unsupported) — gracefully degrade to text-only
                        log.warning(
                            "web_search tool unavailable on %s; degrading to text-only",
                            cred.label,
                        )
                        resp = await client.messages.create(**base_kwargs)
                        return "".join(b.text for b in resp.content if b.type == "text"), False
                else:
                    resp = await client.messages.create(**base_kwargs)
                    return "".join(b.text for b in resp.content if b.type == "text"), False
            except anthropic.APIError as exc:
                # Log the label and error type only — never the key (ARCHITECTURE.md §14.1).
                log.warning(
                    "LLM call failed on %s: %s; trying next credential",
                    cred.label,
                    type(exc).__name__,
                )
                self._record_failure(cred, exc)
                last_error = exc
                continue
        raise RuntimeError("all credentials failed") from last_error

    async def extract_dispatch(
        self,
        system: str,
        user_text: str,
        *,
        tool: dict,
        model: str | None = None,
    ) -> dict | None:
        """Force a single-tool dispatch call and return the tool_use input dict, or None.

        Uses tool_choice={'type':'tool','name':'dispatch'} to guarantee the model
        always calls the named tool. Credential failover follows the same pattern as
        complete() — label logged, key never logged (ARCHITECTURE.md §14.1).
        """
        last_error: Exception | None = None
        for cred in self._ordered():
            client = self._clients[cred.label]
            try:
                resp = await client.messages.create(
                    model=model or self._model,
                    max_tokens=self._max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user_text}],
                    tools=[tool],
                    tool_choice={"type": "tool", "name": tool["name"]},
                )
                for block in resp.content:
                    if block.type == "tool_use":
                        return block.input
                return None
            except anthropic.APIError as exc:
                log.warning(
                    "extract_dispatch failed on %s: %s; trying next credential",
                    cred.label,
                    type(exc).__name__,
                )
                self._record_failure(cred, exc)
                last_error = exc
                continue
        raise RuntimeError("all credentials failed") from last_error
