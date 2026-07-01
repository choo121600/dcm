# Discord Community Manager (dcm)

[![CI](https://github.com/choo121600/dcm/actions/workflows/ci.yml/badge.svg)](https://github.com/choo121600/dcm/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Code of Conduct](https://img.shields.io/badge/Contributor%20Covenant-2.1-4baaaa.svg)](./CODE_OF_CONDUCT.md)

**English** · [한국어](./README.ko.md)

A 24/7 Discord community-management bot — it manages the server (onboarding, roles,
channels, moderation) and converses through a configurable persona (default **썩스가재**;
mention it with `@썩스가재` and it replies in character).
Designed to **remember and grow** while **forgetting the trivial**, like a person.

- Architecture & roadmap: [`ARCHITECTURE.md`](./ARCHITECTURE.md) (한국어: [`ARCHITECTURE.ko.md`](./ARCHITECTURE.ko.md))
- Persona: [`persona.md`](./persona.md)
- Contributing: [`CONTRIBUTING.md`](./CONTRIBUTING.md) · Changelog: [`CHANGELOG.md`](./CHANGELOG.md)

> **Status:** M1–M4 implemented — the bot talks, remembers (importance-weighted recall), forgets
> (time decay + pruning, plus a `잊어줘` command), and grows (reflection → semantic/self memory).
> M5 polish is partial. See ARCHITECTURE.md §11 for the roadmap.

## Setup

### 1. Create the Discord bot
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. **Bot** tab → **Reset Token** → copy it (goes into `.env`).
3. **Enable the Message Content Intent** (Bot tab → Privileged Gateway Intents).
   Without it the bot can't read message text and won't respond. This is the most common setup mistake.
4. **OAuth2 → URL Generator**:
   - **Chat-only:** scope `bot`; permissions **View Channels**, **Send Messages**, **Read Message History**.
   - **With server management:** also add scope `applications.commands` and permissions **Manage Channels** + **Manage Roles**. **Never grant Administrator** (least privilege — ARCHITECTURE.md §14.6–§14.7). Drag the bot's role *above* the roles it should manage.
   Open the generated URL to invite the bot. Server-management slash commands are **admin-only** (callers must hold **Manage Guild**) and register to `ADMIN_GUILD_ID`.

### 2. Install
Requires Python 3.11+.

```bash
git clone <repo> dcm && cd dcm
uv sync            # or: pip install -e .
cp .env.example .env
chmod 600 .env     # keep secrets readable only by you (ARCHITECTURE.md §14.1)
```

### 3. Configure `.env`
```dotenv
DISCORD_TOKEN=...            # from step 1
ANTHROPIC_API_KEY=sk-ant-... # one key, or comma-separated for a key pool
BOT_NAME=썩스가재             # to rename: change this AND the bot's username in the portal
ADMIN_GUILD_ID=...           # server (guild) id for admin slash-command registration (right-click server → Copy Server ID)
```

### 4. Run
```bash
uv run dcm       # or: python -m dcm
```
You should see `썩스가재 online …` and a green status in Discord. Then in the server:
```
@썩스가재 안녕
```

## Running 24/7
`uv run dcm` stops when the terminal closes. To keep it always on, pick one:
- **Home server / Raspberry Pi**: a `systemd` service (auto-restart on crash).
- **Cloud**: fly.io / Railway / a small VPS via a `Dockerfile`.
Either way: restart-on-crash, and (from M2) put the SQLite file on a persistent volume.

## Server templates (`/setup-server`)
Set up a whole server — roles (with permissions), categories, and text/voice channels — from a
single **YAML or JSON** file. Run the admin-only `/setup-server` slash command and attach the
template; the bot shows a preview and applies it after you confirm. Re-running is safe
(**idempotent**: roles/categories/channels that already exist are skipped). **Full guide:**
[docs/server-templates.md](docs/server-templates.md) — schema, permission names, limits, and
ready-to-use examples (YAML & JSON).

```yaml
roles:
  - name: 운영진
    permissions: [manage_channels, manage_roles, kick_members]
categories:
  - name: 2026-summer
    private: true            # visible only to the visible_to roles
    visible_to: [운영진]
    channels:
      - { name: 공지, type: text }
      - { name: 회의, type: voice }
```

For one-off changes you can also just ask in natural language (e.g. `썩스가재야 2026-summer 카테고리 만들어줘`).

## Tests
Offline tests for the memory core and forgetting (no keys/network needed):
```bash
PYTHONPATH=src python tests/test_memory.py
PYTHONPATH=src python tests/test_forgetting.py
```

## Security notes
- `.env` is git-ignored — never commit real secrets. Keys are never written to logs.
- The bot makes outbound connections only (no inbound ports / web server).
- Invite it only to the channels it needs. See ARCHITECTURE.md §14 for the full security model.

## Localization
The bot's language is configurable. User-facing strings live in `src/dcm/i18n/locales/`
(`en.yaml`, `ko.yaml`, plus per-namespace fragments under `en/` and `ko/`); pick one with
`BOT_LOCALE` (default `ko`). To add a language, copy a locale's files, translate the values, and
set `BOT_LOCALE` to its code. See [`ARCHITECTURE.md`](./ARCHITECTURE.md) §10 for the design.

## Customization
A few things are intentionally deployment-specific — swap them for your own community:
- **`persona.md`** — the bot's character (its example lines are in the persona's language).
- **`knowledge.md`** — static server/community knowledge injected into the prompt (`KNOWLEDGE_FILE`).
- The study-name lookup in `src/dcm/service/study_lookup.py` is tailored to one community's data.

## Contributing
Contributions are welcome! See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for local setup, the
checks we run (`pytest` + `ruff`), and project conventions. Please also read our
[Code of Conduct](./CODE_OF_CONDUCT.md).

## License
[MIT](./LICENSE) © Yeonguk
