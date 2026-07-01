# Architecture — Discord Community Manager (dcm)

> 🌏 한국어 버전: [ARCHITECTURE.ko.md](./ARCHITECTURE.ko.md)
>
> This document is the design reference for dcm. Section anchors (e.g. `§14.1`) are cited
> from code comments throughout `src/` — keep the numbering stable when editing.

## 1. Overview

dcm is a 24/7 Discord community-management bot. It does two jobs at once:

1. **Manages the server** — onboarding, roles, categories/channels, moderation, cleanup,
   announcements, and one-shot server setup from a template file.
2. **Converses through a persona** — mention it and it replies in character, and it
   **remembers and grows** over time while **forgetting the trivial**, like a person.

Design priorities, in order: *stay up* (graceful degradation, restart-safe state),
*stay safe* (least privilege, no secrets in logs, prompt-injection resistance), and
*stay portable* (a small, library-agnostic core behind a thin platform adapter).

## 2. Platform- and library-agnostic core

The conversational core (`orchestrator.py`) is independent of Discord and of any specific
chat library. It receives plain data (author, text, recent buffer) and returns a reply
string; it never imports `discord`. This keeps the interesting logic unit-testable offline
and lets the bot be re-hosted on another platform by writing one adapter (§3). Memory is
optional: with no store/embedder wired in, the orchestrator still converses.

## 3. Chat-platform isolation boundary

`platform/base.py` defines a `ChatPlatform` protocol and the `AuthContext` primitive; it is
the **isolation seam**. `platform/pycord_adapter.py` is the *only* module that imports
`discord` — it translates gateway events into core calls and renders core results back into
Discord messages, embeds, buttons, and slash commands.

### 3.1 Server-management surface

Privileged actions (create/edit/delete categories, channels, roles; moderation; template
apply; cleanup) are exposed two ways that share one authorization + confirmation path:

- **Slash commands** registered to `ADMIN_GUILD_ID`.
- **Natural language** — the NL router (`agent/router.py`) parses a mention into a closed
  verb set and dispatches to the same service layer.

Both go through `guild_admin.py`, which gates high-risk operations behind an explicit
preview → confirm step. Authorization is role-based (`ADMIN_ROLE_ID`) with the guild owner
always allowed.

## 4. Persona (the fixed-identity layer)

`persona.md` **is** the fixed-identity layer of the system prompt. It is human-edited and
injected verbatim by the orchestrator, which then appends the grown self-memory, retrieved
memories, and the recent conversation buffer. The persona file is written in English by
convention; its example lines are in the bot's runtime language because that is what the bot
speaks in the server. Re-theming the bot is a single-file edit.

Evolving traits do **not** live here — they live in the bot's self-memory (§5.6). This layer
stays stable so the character is consistent.

## 5. Memory — remember, and forget, like a person

### 5.1 Memory types
- **Episodic** — individual remembered exchanges (who said what, when).
- **Semantic** — consolidated facts distilled from episodics.
- **Self** — the bot's evolving self-model (its own traits, appended to the persona).

### 5.2 Flow
On each handled mention the orchestrator retrieves the most relevant memories (§5.4), builds
the prompt, and replies. Storing new memories happens **off the response path** so latency
and cost never block the reply.

### 5.3 Ingestion
`memory/ingest.py` turns a finished exchange into stored memories using a cheap model (§12),
asynchronously after the reply is sent.

### 5.4 Retrieval & scoring
Retrieval ranks candidates by a weighted sum of **relevance + recency + importance**
(`memory/scoring.py`; weights such as `w_rel` are configurable, §9). Semantic similarity uses
the configured embedding provider (`local` for offline/non-semantic testing, or a real
provider like Voyage/OpenAI).

### 5.5 Forgetting
Memory genuinely fades. Each memory has a **half-life that grows with importance**, so trivia
decays quickly and important things linger (`memory/forgetting.py`, `memory/scoring.py`). A
periodic prune (§7) archives then deletes low-retention memories. Forgetting is **irreversible
in the live store but audited**: deletions are logged to a `forgotten_memories` archive (§6, §12).

### 5.6 Reflection & growth
`memory/reflection.py` periodically consolidates episodics into semantic and self memories,
then reduces the importance of the consolidated sources so they fade — the bot "learns" and
lets the raw material go.

## 6. Storage

State is **SQLite**, so a single-host deployment needs no external database. Schemas live in
`memory/schema.sql` and `leveling/schema.sql`.

- **Lock-domain isolation** — memory and leveling use **separate database files**
  (`MEMORY_DB`, `LEVELING_DB`) so a hot write path can't contend with the other's lock.
- **Multi-guild scoping** — stores are guild-scoped so data from one server never leaks into
  another (`_GuildScopedStore`).
- **Restart-safe** — put the SQLite files on a persistent volume; the bot resumes cleanly.

## 7. Background jobs

`scheduler.py` runs periodic memory maintenance: **pruning** (forgetting, §5.5) and
**reflection** (growth, §5.6), on independent intervals (`PRUNE_INTERVAL_HOURS`,
`REFLECT_INTERVAL_HOURS`). Jobs degrade silently on error and never take down the chat path.

## 8. Component map

```
Discord gateway
      │
platform/pycord_adapter.py ── the only module importing `discord`  (§3)
      │            │
      │            └── agent/router.py ──▶ service/*  (guild_admin, template,
      │  (NL verbs)                        announcements, cleanup, onboarding,
      │                                    leveling, study_lookup, copywriter)
      ▼
orchestrator.py  ── library-agnostic core  (§2)
      │
      ├── llm.py            Anthropic calls + key pool  (§9.1)
      ├── memory/*          store, ingest, retrieve, forget, reflect  (§5, §6)
      ├── i18n/*            locale catalogs for user-facing strings  (§10)
      └── persona.md        fixed-identity layer  (§4)
```

## 9. Runtime configuration

All configuration loads from the environment / `.env` via `config.py` (pydantic-settings).
See `.env.example` for the full list. Notable knobs: `MODEL`, `INGEST_MODEL`, `MAX_TOKENS`,
`MAX_INPUT_CHARS`, `RECENT_BUFFER_SIZE`, memory weights and half-life, background-job intervals,
and `BOT_LOCALE` (§10).

### 9.1 LLM credentials & key pool
`llm.py` wraps Anthropic behind a **credential list + selection strategy**. `ANTHROPIC_API_KEY`
accepts one key or several comma-separated keys forming a **pool** (spreads rate limits). Each
credential carries a non-secret `label`; **the key value is never logged** — only the label
(§14.1).

## 10. Internationalization (i18n)

The bot's user-facing strings are **externalized from source** into locale catalogs so the bot
can speak a chosen language without code changes.

- Catalogs: `src/dcm/i18n/locales/en.yaml` and `ko.yaml` (dotted keys, `{param}` placeholders).
- Lookup: `t("key", **params)` resolves against the active locale, falling back to the default.
- Selection: the `BOT_LOCALE` setting (default `ko`, which preserves the original behavior).

Not everything is a translatable string. Three categories are distinct:
1. **Display strings** → locale catalogs (translate).
2. **LLM prompt scaffolding** → English instructions with a "reply in `<locale>`" directive;
   the persona voice itself comes from `persona.md` (§4).
3. **Input-matching data** (NL trigger words, regexes, live Discord object names) → *functional*
   and locale-scoped; it parses user input, so it must match the user's language rather than be
   naively translated.

## 11. Roadmap

Milestones:
- **M1** — conversation through the persona. ✅
- **M2** — memory: importance-weighted recall. ✅
- **M3** — forgetting: time decay + pruning, plus a self-service "forget me" command. ✅
- **M4** — growth: reflection into semantic/self memory. ✅
- **M5** — polish (partial).

Server-management track (**ralplan S1–S7**): admin registration, role-based authz, channel/role/
category ops, moderation, template apply, onboarding, and the live-guild smoke step. Activity
**leveling** (G001–G004): XP scoring, decay, quotas, and anti-abuse gating.

## 12. Reliability & graceful degradation

- **Never hard-fail the chat path.** If the LLM is unavailable, the orchestrator returns a
  persona-voiced fallback instead of an error.
- **Cheap where it can be.** Ingestion/classification use a cheaper model (`INGEST_MODEL`) than
  conversation.
- **Audited forgetting.** Deletions are archived (§5.5) before removal.
- **Restart-on-crash is the host's job** (systemd/PaaS); state is restart-safe (§6).

## 13. Command & interaction surface

Users interact by **mentioning** the bot (conversation + NL management) or via **slash commands**
(management, leveling, cleanup, announcements, `/setup-server`). Admin actions require the admin
role or guild ownership and, when high-risk, an explicit confirmation (§3.1).

### 13.5 Self-service memory commands
Lightweight intent detection (`commands.py`) lets any user ask the bot, in natural language, to
**show** what it remembers about them or to **forget** them — a privacy affordance (§14.2). It is
regex/keyword based and locale-scoped (§10), deliberately narrow to avoid false triggers.

## 14. Security model

### 14.1 Secrets & logging
`.env` is git-ignored; real secrets are never committed. API keys are **never written to logs** —
only non-secret labels. As defense in depth, `logging_setup.py` installs a `SecretRedactor` that
scrubs anything shaped like an Anthropic key or Discord token from every log record.

### 14.2 Privileged intents
The bot requires the **Message Content** and **Server Members** privileged gateway intents; the
code always requests them, so if they are disabled in the Developer Portal the bot **fails loudly**
at startup (a restart loop) rather than silently misbehaving. On `on_ready` it logs which
privileged intents are active for operator verification.

### 14.3 Prompt-injection resistance
The system prompt instructs the model to ignore messages that try to override the persona or
reveal the instructions. Attachments handed to `/setup-server` are treated as **data only** (sent
to the parser, never stored to memory or conversation).

### 14.4 Rate & size caps
Cost and abuse are bounded by an input cap (`MAX_INPUT_CHARS`), a response cap (`MAX_TOKENS`), and
a **per-user mention cooldown** (`COOLDOWN_SECONDS`). An anti-monopoly nudge keeps one person from
dominating the bot in a public channel.

### 14.5 No inbound ports
The bot makes **outbound connections only** (Discord gateway + Anthropic API). There is no inbound
port and no web server — nothing to expose or firewall. If you ever add a metrics/health endpoint,
bind it to an internal address, never `0.0.0.0`.

### 14.6 Least privilege — never Administrator
Never grant the bot **Administrator**. A stolen token with Administrator endangers the whole
server. Grant only the specific permissions it needs (Manage Channels, Manage Roles, and the
moderation permissions if used).

### 14.7 Role hierarchy
Discord can only manage roles **below** the bot's own role. Drag the bot's role *above* the roles
it should manage. All bot-made changes are attributed in the server Audit Log to the requesting
user.
