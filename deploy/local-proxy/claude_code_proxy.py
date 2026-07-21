#!/usr/bin/env python3
"""Minimal Anthropic /v1/messages proxy backed by headless Claude Code (`claude -p`).

Runs NATIVELY on your laptop (NOT in Docker) so it uses the host's Claude Code **subscription**
auth. Docker can't reach the macOS Keychain/OAuth session, which is why this is a host process.
dcm's bot (on the SSH server) dials it over Tailscale and, with PREFER_PROXY=true, uses it first;
when the laptop is off the bot falls back to ANTHROPIC_API_KEY (ARCHITECTURE.md §9.1).

Honest scope (partial coverage by design):
  - Plain conversation  → served by your Claude subscription (full Claude quality).
  - tools / tool_choice → NOT supported here (Claude Code isn't a raw Messages API). Such calls
    return HTTP 400 so the bot transparently fails over: web_search degrades to text on this proxy,
    and the NL router's forced tool_use falls back to the API key.
  - Any claude error / timeout → HTTP 5xx so the bot fails over to the API key.

Policy: as of the June 15 2026 reversal, programmatic/third-party usage draws from your normal
Pro/Max subscription limits again — but this is volatile and abuse/high-volume patterns still risk
account action. Keep volume sane; you can disable instantly by unsetting PREFER_PROXY on the server.

Env:
  PROXY_HOST         bind address (default 0.0.0.0 so a tailnet peer can reach it; set to your
                     tailnet IP e.g. 100.66.194.81 to bind only Tailscale)
  PROXY_PORT         listen port (default 8787)
  PROXY_AUTH_TOKEN   optional shared secret; when set, requests must present it as x-api-key/Bearer
  CLAUDE_BIN         claude binary (default: claude on PATH)
  PROXY_TIMEOUT      per-request seconds before giving up on claude (default 180)

Run:  claude login    # once, so the subscription session exists
      python3 claude_code_proxy.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
HOST = os.environ.get("PROXY_HOST", "0.0.0.0")
PORT = int(os.environ.get("PROXY_PORT", "8787"))
AUTH_TOKEN = os.environ.get("PROXY_AUTH_TOKEN", "")
TIMEOUT = float(os.environ.get("PROXY_TIMEOUT", "180"))

# A neutral empty cwd so Claude Code doesn't auto-discover a project CLAUDE.md and leak its context.
_WORKDIR = tempfile.mkdtemp(prefix="dcm-claude-proxy-")


def _err(kind: str, message: str) -> dict:
    return {"type": "error", "error": {"type": kind, "message": message}}


def _map_model(model: str) -> str:
    """Map the bot's dated model id onto a Claude Code alias."""
    m = (model or "").lower()
    for alias in ("haiku", "sonnet", "opus"):
        if alias in m:
            return alias
    return model or "sonnet"


def _text_of(content) -> str:
    """Anthropic content may be a string or a list of blocks; keep only text."""
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return content or ""


def _flatten(messages) -> str:
    lines = []
    for msg in messages or []:
        who = "User" if msg.get("role") == "user" else "Assistant"
        lines.append(f"{who}: {_text_of(msg.get('content'))}")
    return "\n".join(lines)


def _run_claude(system: str, prompt: str, model: str) -> str:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # force subscription (OAuth); never bill the API key here
    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "text",
        "--model", _map_model(model),
        "--tools", "",                 # pure chat: no file/bash/edit tools
        "--no-session-persistence",
    ]
    if system:
        cmd += ["--system-prompt", system]
    proc = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True,
        timeout=TIMEOUT, env=env, cwd=_WORKDIR,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        raw = self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        if AUTH_TOKEN:
            got = self.headers.get("x-api-key") or self.headers.get(
                "authorization", ""
            ).removeprefix("Bearer ").strip()
            if got != AUTH_TOKEN:
                return self._send(401, _err("authentication_error", "invalid proxy token"))
        if not self.path.rstrip("/").endswith("/v1/messages"):
            return self._send(404, _err("not_found_error", f"unknown path {self.path}"))
        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._send(400, _err("invalid_request_error", "invalid json body"))
        # Claude Code is not a raw Messages API: reject tool calls so the bot fails over (§9.1).
        if req.get("tools") or req.get("tool_choice"):
            return self._send(
                400, _err("invalid_request_error", "tools unsupported by claude-code proxy")
            )
        system = _text_of(req.get("system"))
        prompt = _flatten(req.get("messages"))
        try:
            text = _run_claude(system, prompt, req.get("model", ""))
        except subprocess.TimeoutExpired:
            return self._send(504, _err("timeout_error", "claude timed out"))
        except Exception as exc:  # any claude failure → 502 so the bot uses the API key
            return self._send(502, _err("api_error", str(exc)[:300]))
        self._send(200, {
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": req.get("model") or "claude-code",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })

    def log_message(self, *a) -> None:  # quiet; the bot logs its own routing
        pass


def main() -> None:
    if not shutil.which(CLAUDE_BIN):
        raise SystemExit(f"'{CLAUDE_BIN}' not on PATH — install Claude Code and run `claude login`")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(
        f"claude-code proxy → http://{HOST}:{PORT}/v1/messages "
        f"(backend={CLAUDE_BIN}, subscription auth, cwd={_WORKDIR})"
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
