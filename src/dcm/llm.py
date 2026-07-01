from __future__ import annotations

import logging
from dataclasses import dataclass

import anthropic

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Credential:
    """One LLM credential. The key value is never logged — only `label` (ARCHITECTURE.md §9.1)."""

    api_key: str
    label: str
    org: str | None = None


def parse_credentials(raw: str) -> list[Credential]:
    """Parse a comma-separated key string into the credential list (one key = pool of 1)."""
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return [Credential(api_key=k, label=f"key{i + 1}") for i, k in enumerate(keys)]


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
        self._clients = {
            c.label: anthropic.AsyncAnthropic(api_key=c.api_key) for c in creds
        }

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
        for cred in self._creds:  # M1: single pass; pool: failover order
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
        for cred in self._creds:
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
                last_error = exc
                continue
        raise RuntimeError("all credentials failed") from last_error
