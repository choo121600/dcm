# DCM Deployment Guide (24/7 + Remote Management)

> 🌏 한국어 버전: [README.ko.md](./README.ko.md)

DCM uses **outbound connections only** (Discord gateway + Anthropic API) — no inbound ports, no web server (ARCHITECTURE.md §14.5).

---

## Hard Cutover Checklist

Before the first production startup, verify the items below in order.

### 1. Set required .env values

```bash
# From the WorkingDirectory (/opt/dcm), pydantic-settings loads .env automatically.
cp .env.example .env
nano .env
chmod 600 /opt/dcm/.env   # Readable only by the service user (ARCHITECTURE.md §14.1)
```

**Required (startup fails if unset):**

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Developer Portal → App → Bot → Reset Token |
| `ANTHROPIC_API_KEY` | Anthropic console API key (comma-separated list allowed) |
| `ADMIN_GUILD_ID` | Discord server ID where slash commands are registered |
| `ADMIN_ROLE_ID` | Role ID that holds admin command permissions |

**Optional (set when enabling onboarding features):**

| Variable | Description |
|---|---|
| `BOT_NAME` | Bot display name (default: `썩스가재`) |
| `WELCOME_CHANNEL_ID` | Channel ID for sending the new-member welcome message |
| `WELCOME_MESSAGE` | Welcome message text |
| `DEFAULT_ROLE_ID` | Role ID automatically granted to new members |

### 2. Discord Developer Portal — Enable Privileged Intents

In Developer Portal → App → Bot → **Privileged Gateway Intents**, be sure to enable both of the items below.

- ✅ **Message Content Intent** — required for parsing mention bodies
- ✅ **Server Members Intent** — required for `on_member_join` firing and onboarding

> **Warning:** The code always requests both intents, so if either is disabled in the Developer Portal, the bot
> **fails immediately** on startup with a `PrivilegedIntentsRequired` exception (a systemd restart loop) — it fails loudly, not silently.
> On a healthy startup, `on_ready` logs the active intents, so verify them in §5.

### 3. Least-privilege principle for the bot role and permissions

When inviting the bot to a Discord server, **never grant Administrator permission.**

**No Administrator** — if the token is stolen, the entire server is exposed to risk (ARCHITECTURE.md §14.6).

Minimum required permissions:

- Manage Channels
- Manage Roles
- Kick Members
- Ban Members
- Moderate Members (timeout)

**Drag the bot role above the roles it manages.** In the Discord role hierarchy, the bot cannot act on roles ranked higher than its own.

### 4. Token rotation

Right before cutover, refresh the bot token via Developer Portal → App → Bot → **Reset Token**, replace `DISCORD_TOKEN` in `.env`, then restart the service. Rotate immediately if a leak is suspected.

```bash
sudo systemctl restart dcm
```

### 5. Live-guild startup smoke test (operator manual step)

> This step is a human manual verification step that requires the **operator's DISCORD_TOKEN**.
> It cannot be automated in a CI/offline environment.

After startup, verify:

```bash
journalctl -u dcm -f   # live log stream
```

In the on_ready logs, verify the following two lines:

```
privileged intent message_content=True
privileged intent members=True
```

If healthy, both lines are printed and the bot reaches `on_ready` (= confirmation that both privileged intents are active). If the logs do not appear and you instead see a `PrivilegedIntentsRequired` exception with a restart loop, enable the relevant intent (especially Server Members) in the Developer Portal and restart.

---

## 1. Initial host setup

```bash
sudo useradd --system --create-home --home-dir /opt/dcm dcm
sudo -u dcm git clone <repo> /opt/dcm
cd /opt/dcm
sudo -u dcm python3 -m venv .venv
sudo -u dcm .venv/bin/pip install -e .

sudo -u dcm cp .env.example .env
sudo -u dcm nano .env          # See checklist §1 above
sudo chmod 600 /opt/dcm/.env
sudo -u dcm mkdir -p /opt/dcm/data
```

## 2. Install the service

```bash
sudo cp deploy/dcm.service /etc/systemd/system/dcm.service
sudo systemctl daemon-reload
sudo systemctl enable --now dcm   # Start immediately + auto-start on boot
```

## 3. Remote management

```bash
systemctl status dcm
journalctl -u dcm -f              # Live logs (keys are not exposed in logs, per §14.1)
sudo systemctl restart dcm        # After an .env change or code update

# Update:
cd /opt/dcm && sudo -u dcm git pull && sudo -u dcm .venv/bin/pip install -e . \
  && sudo systemctl restart dcm
```

## 4. Local-first LLM routing (optional)

Run the LLM through a **proxy on your laptop when it's online**, and fall back to the server's
`ANTHROPIC_API_KEY` **only when the laptop is off** (ARCHITECTURE.md §9.1). The bot itself stays a
**single instance on this server** — do *not* run a second live bot on the laptop (one Discord
gateway session per token; two writers would double-reply and split the SQLite state, §6).

**How it works.** `LLMClient` tries credentials in order and fails over on any error, *including a
connection error*. With `PREFER_PROXY=true` the proxy credential is placed **first**:

- Laptop **on** → the proxy answers (cheap/local).
- Laptop **off** → the proxy connection fails fast (short `PROXY_CONNECT_TIMEOUT`), the call fails
  over to `ANTHROPIC_API_KEY`, and a circuit breaker skips the proxy for `PROXY_BREAKER_COOLDOWN`
  seconds so later messages go straight to the API key with no per-message latency.

### 4.1 On the laptop — run the proxy over Tailscale

The proxy is reachable from this server over [Tailscale](https://tailscale.com), so the laptop
opens **no inbound LAN ports**. A ready-made compose stack (proxy + Tailscale sidecar) lives in
[`deploy/local-proxy/`](./local-proxy/):

```bash
cd deploy/local-proxy
cp .env.example .env          # set TS_AUTHKEY + PROXY_IMAGE (+ your proxy's own settings)
docker compose up -d
docker compose exec tailscale tailscale ip -4   # note the tailnet IP (or use the MagicDNS name)
```

> The proxy must speak the **Anthropic Messages API** (`/v1/messages`) on port `8787` and accept
> both `MODEL` and `INGEST_MODEL`. **Policy note (§9.1):** a second real key or a Bedrock/Vertex
> gateway behind it is safe; fronting a Claude subscription token for an always-on bot can violate
> Anthropic's usage policy — your call what you put behind `PROXY_IMAGE`.

### 4.2 On this server — prefer the proxy

Add to `/opt/dcm/.env`, then restart:

```bash
PREFER_PROXY=true
FALLBACK_BASE_URL=http://dcm-local-proxy:8787   # MagicDNS name, or http://<laptop-tailnet-ip>:8787
FALLBACK_API_KEY=                               # token the proxy expects; empty → reuse ANTHROPIC_API_KEY
# PROXY_CONNECT_TIMEOUT=2.0                      # fast-failover connect bound (default 2s)
# PROXY_BREAKER_COOLDOWN=30                      # skip the proxy for N s after a connection failure
```

```bash
sudo systemctl restart dcm
journalctl -u dcm -f   # startup logs "LLM routing: local proxy preferred, API key fallback"
```

Leave `PREFER_PROXY` unset (or `false`) to keep the classic behavior where `FALLBACK_BASE_URL` is a
**last-resort** endpoint tried only after the real key errors.

## Notes

- **24/7 is the host's responsibility**: Make sure the server does not go into sleep mode. systemd restarts the bot on crash/reboot.
- **No inbound ports**: The bot uses outbound connections only. No firewall rules need to be added.
- **When adding a metrics/health endpoint**: Bind only to an internal address, not `0.0.0.0`, to maintain the "no inbound ports" principle (ARCHITECTURE.md §14.5).
