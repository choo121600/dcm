# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- English-primary documentation with Korean sub-documents (`*.ko.md`): `README`,
  `docs/server-templates`, `deploy/README`.
- `ARCHITECTURE.md` (+ `ARCHITECTURE.ko.md`) documenting the persona, memory,
  LLM, roadmap, and security model — replacing the previously dangling `DESIGN.md` references.
- Internationalization (i18n) layer: `src/dcm/i18n/` with `locales/en.yaml` and
  `locales/ko.yaml`, selectable via the `BOT_LOCALE` setting (default `ko`).
- Open-source project scaffolding: `LICENSE` (MIT), `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, GitHub issue/PR templates, and a CI workflow (ruff + pytest).

### Changed
- Code comments and docstrings translated to English throughout `src/`.
- User-facing bot strings externalized from source into locale catalogs.

## [0.1.0] - 2026

### Added
- Initial release: 24/7 Discord community-management bot with a configurable persona.
- Milestones M1–M4: conversation, importance-weighted memory recall, time-decay
  forgetting + pruning, and reflection-driven growth (semantic/self memory).
- Server templates (`/setup-server`), activity leveling, onboarding, announcements,
  moderation, and cleanup subsystems.

[Unreleased]: https://github.com/choo121600/dcm/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/choo121600/dcm/releases/tag/v0.1.0
