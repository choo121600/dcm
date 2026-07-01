# AGENTS.md

Rules every agent (Claude, etc.) working in this repository must follow.

## Git / commits

- The commit author/committer must be **the account owner only (Yeonguk <choo121600@gmail.com>)**.
  Do not list Claude or any other agent as the author.
- Do not add any agent as a co-author of a commit message or PR body.
  Specifically, **never add**:
  - a `Co-Authored-By: Claude ...` trailer
  - a `Claude-Session: ...` trailer
  - "🤖 Generated with Claude Code"-style phrasing in a PR body

## Language & docs

- Documentation and code comments are written in **English** (global-standard, English-primary).
- Korean translations live in `*.ko.md` sibling files (e.g. `README.ko.md`). Keep them in sync when
  you change the English source.
- The bot's **runtime voice** is intentionally localizable. User-facing strings live in
  `src/dcm/i18n/locales/{en,ko}.yaml`, not inline in source. Add new user-facing text as an i18n
  key in **both** locales rather than hard-coding a string.

## Before you commit

- Run the test suite and linter: `pytest -q` and `ruff check .` (see `CONTRIBUTING.md`).
