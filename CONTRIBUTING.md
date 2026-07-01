# Contributing to dcm

Thanks for your interest in improving **Discord Community Manager (dcm)**! This guide
covers local setup, the checks we run, and conventions specific to this project.

By participating you agree to abide by our [Code of Conduct](./CODE_OF_CONDUCT.md).

## Development setup

Requires **Python 3.11+** and [uv](https://docs.astral.sh/uv/) (recommended).

```bash
git clone https://github.com/choo121600/dcm.git && cd dcm
uv sync                 # create the venv and install deps (incl. dev tools)
cp .env.example .env    # fill in tokens only if you want to run the live bot
```

You do **not** need Discord or Anthropic keys to run the test suite — the memory,
forgetting, leveling, and template subsystems have offline tests.

## Running checks

We keep `main` green. Before opening a PR, run both:

```bash
uv run pytest -q        # 544+ tests, fully offline
uvx ruff check .        # lint (import order, common bugs)
```

`ruff` line length is intentionally not enforced (prompt strings are prose-heavy).
Auto-fixable issues: `uvx ruff check --fix .`.

## Project conventions

### Language & internationalization
- **Docs and code comments are English.** Korean documentation lives in `*.ko.md`
  sibling files (e.g. `README.ko.md`); keep them in sync when you touch the English source.
- **The bot's runtime voice is localized, not hard-coded.** User-facing strings live in
  `src/dcm/i18n/locales/{en,ko}.yaml`. When you add or change a user-facing message, add
  the key to **both** locale files rather than embedding a literal string in `src/`.
- Korean **input-matching** data (NL trigger words, regexes, live Discord object names) is
  functional and locale-scoped — see `ARCHITECTURE.md` before editing it.

### Architecture
Read [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the persona/memory/LLM/security model. Section
anchors (e.g. `ARCHITECTURE.md §14.1`) are referenced from code comments — keep them stable.

### Security
- Never commit secrets. `.env` is git-ignored; keys are redacted from logs (`§14.1`).
- The bot makes outbound connections only — no inbound ports or web server.
- Prefer least-privilege Discord permissions; never require `Administrator`.

## Commit & PR flow

1. Branch from `main` (`git switch -c feat/my-change`).
2. Make focused commits. The commit author must be **you** — do not add agent co-author or
   `Co-Authored-By`/`Generated with` trailers (see [`AGENTS.md`](./AGENTS.md)).
3. Ensure `pytest` and `ruff` pass, and update docs / locale files as needed.
4. Open a PR against `main`; the template checklist must be satisfied. CI runs ruff + pytest
   on Python 3.11–3.13.

## Reporting bugs & requesting features

Use the [issue templates](https://github.com/choo121600/dcm/issues/new/choose). For questions,
open a Discussion. Please never paste tokens or `.env` contents into an issue.
