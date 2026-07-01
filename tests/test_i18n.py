"""Tests for the i18n layer (ARCHITECTURE.md §10)."""

from __future__ import annotations

import string

import pytest

from dcm import i18n


@pytest.fixture(autouse=True)
def _restore_locale():
    original = i18n.get_locale()
    yield
    i18n.set_locale(original)


def test_available_locales_includes_en_and_ko():
    locales = i18n.available_locales()
    assert "ko" in locales
    assert "en" in locales


def test_default_locale_is_ko():
    # Default preserves the bot's original language.
    assert i18n.DEFAULT_LOCALE == "ko"
    assert i18n.FALLBACK_LOCALE == "ko"


def test_missing_key_returns_key_itself():
    assert i18n.t("totally.unknown.key") == "totally.unknown.key"


def test_locale_switch_and_fallback():
    i18n.set_locale("en")
    # A key present in both resolves to the English value...
    assert i18n.t("_meta.name") == "English"
    # ...and a key missing from en falls back to the ko reference catalog.
    ko_only = _ko_only_keys()
    if ko_only:
        key = ko_only[0]
        assert i18n.t(key) == i18n.t(key, locale="ko")


def test_every_english_key_exists_in_ko_reference():
    """`ko` is the reference catalog; every en key must have a ko counterpart."""
    ko_keys = set(i18n._catalog("ko"))
    en_keys = set(i18n._catalog("en"))
    missing = sorted(en_keys - ko_keys)
    assert not missing, f"en.yaml keys absent from ko.yaml reference: {missing}"


def test_ko_and_en_have_identical_keys():
    """Both shipped locales must be complete translations of each other (no gaps)."""
    ko_keys = set(i18n._catalog("ko"))
    en_keys = set(i18n._catalog("en"))
    only_ko = sorted(ko_keys - en_keys)
    only_en = sorted(en_keys - ko_keys)
    assert not only_ko and not only_en, (
        f"locale key sets differ — ko-only: {only_ko}; en-only: {only_en}"
    )


def test_placeholders_match_between_locales():
    """A translated string must use the same {named} placeholders as its ko reference."""
    ko = i18n._catalog("ko")
    en = i18n._catalog("en")
    mismatched = []
    for key, en_val in en.items():
        if key not in ko or not isinstance(en_val, str):
            continue
        if _placeholders(en_val) != _placeholders(ko[key]):
            mismatched.append(key)
    assert not mismatched, f"placeholder mismatch between en and ko for: {mismatched}"


def _ko_only_keys() -> list[str]:
    ko_keys = set(i18n._catalog("ko"))
    en_keys = set(i18n._catalog("en"))
    return sorted(ko_keys - en_keys)


def _placeholders(value: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(value) if name}
