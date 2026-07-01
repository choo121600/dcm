"""Internationalization (i18n) for dcm's user-facing strings (ARCHITECTURE.md §10).

The bot's runtime voice is externalized into per-locale YAML catalogs under `locales/`
so the language can be chosen without editing source. Use :func:`t` to resolve a dotted
key against the active locale, e.g. ``t("template.preview_title")`` or
``t("guild_admin.category_created", name=name, cid=cid)``.

`ko` is the complete reference catalog (the bot's original language) and the fallback for
any key missing from another locale, so partial translations degrade gracefully rather
than crashing. The active locale defaults to `ko` (set from the `BOT_LOCALE` setting), which
preserves the bot's original behavior out of the box.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml

_LOCALES_DIR = Path(__file__).parent / "locales"
FALLBACK_LOCALE = "ko"
DEFAULT_LOCALE = "ko"

_active_locale = DEFAULT_LOCALE


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten a nested catalog into dotted keys: ``{"a": {"b": "x"}}`` -> ``{"a.b": "x"}``."""
    out: dict[str, str] = {}
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(_flatten(value, dotted))
        else:
            out[dotted] = value
    return out


def _load_yaml(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8") as handle:
        return _flatten(yaml.safe_load(handle) or {})


@functools.lru_cache(maxsize=None)
def _catalog(locale: str) -> dict[str, str]:
    """Merge a locale's base file and its per-namespace fragments into one flat dict.

    A locale ``ko`` is assembled from ``locales/ko.yaml`` (optional base) plus every
    ``locales/ko/*.yaml`` fragment. Splitting by namespace lets independent areas own
    their own catalog file without contending on a single monolithic YAML.
    """
    catalog: dict[str, str] = {}
    base = _LOCALES_DIR / f"{locale}.yaml"
    if base.exists():
        catalog.update(_load_yaml(base))
    fragment_dir = _LOCALES_DIR / locale
    if fragment_dir.is_dir():
        for fragment in sorted(fragment_dir.glob("*.yaml")):
            catalog.update(_load_yaml(fragment))
    return catalog


def available_locales() -> list[str]:
    """Locale codes for which a catalog file or fragment directory exists."""
    codes = {p.stem for p in _LOCALES_DIR.glob("*.yaml")}
    codes.update(p.name for p in _LOCALES_DIR.iterdir() if p.is_dir())
    return sorted(codes)


def set_locale(locale: str) -> None:
    """Set the process-wide active locale (typically once at startup from `BOT_LOCALE`)."""
    global _active_locale
    _active_locale = locale or DEFAULT_LOCALE


def get_locale() -> str:
    return _active_locale


def language_name(locale: str | None = None) -> str:
    """English name of the locale's language (e.g. ``"Korean"``), for use in LLM prompts."""
    loc = locale or _active_locale
    return _catalog(loc).get("_meta.language") or _catalog(FALLBACK_LOCALE).get(
        "_meta.language", "Korean"
    )


def t(key: str, /, locale: str | None = None, **params: Any) -> str:
    """Resolve a dotted i18n key for the active (or given) locale, formatting with `params`.

    Falls back to the `ko` reference catalog for missing keys, and to the raw key itself if
    the string is defined nowhere — so a lookup never raises.
    """
    loc = locale or _active_locale
    value = _catalog(loc).get(key)
    if value is None and loc != FALLBACK_LOCALE:
        value = _catalog(FALLBACK_LOCALE).get(key)
    if value is None:
        return key
    if params:
        try:
            return value.format(**params)
        except (KeyError, IndexError, ValueError):
            return value
    return value
