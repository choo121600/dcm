"""S1 config tests (ralplan): new admin-role / onboarding settings + required validation.

Run: .venv/bin/python -m pytest tests/test_config.py -q
"""
import pytest
from pydantic import ValidationError

from dcm.config import Settings

# Env vars Settings may read; cleared so tests are hermetic (kwargs are the only source).
_ENV_KEYS = (
    "DISCORD_TOKEN", "ANTHROPIC_API_KEY", "ADMIN_GUILD_ID", "ADMIN_ROLE_ID",
    "WELCOME_CHANNEL_ID", "WELCOME_MESSAGE", "DEFAULT_ROLE_ID", "BOT_NAME",
    "FALLBACK_BASE_URL", "FALLBACK_API_KEY",
    "PREFER_PROXY", "PROXY_CONNECT_TIMEOUT", "PROXY_BREAKER_COOLDOWN",
)

_BASE = dict(discord_token="t", anthropic_api_key="k", admin_guild_id=111, admin_role_id=222)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_loads_with_required_fields():
    s = Settings(_env_file=None, **_BASE)
    assert s.admin_guild_id == 111
    assert s.admin_role_id == 222
    # onboarding optional → graceful defaults
    assert s.welcome_channel_id is None
    assert s.default_role_id is None
    # Empty by default → onboarding falls back to the active locale's default greeting (§10).
    assert isinstance(s.welcome_message, str)


def test_admin_role_id_is_required():
    # Missing admin_role_id must raise a clear boot-time validation error.
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, discord_token="t", anthropic_api_key="k", admin_guild_id=111)
    assert "admin_role_id" in str(exc.value)


def test_onboarding_fields_set_when_provided():
    s = Settings(
        _env_file=None,
        **_BASE,
        welcome_channel_id=333,
        default_role_id=444,
        welcome_message="hi there",
    )
    assert s.welcome_channel_id == 333
    assert s.default_role_id == 444
    assert s.welcome_message == "hi there"


def test_role_ids_coerced_to_int():
    # Discord snowflake ids arrive as strings from env; pydantic coerces to int.
    s = Settings(_env_file=None, discord_token="t", anthropic_api_key="k",
                 admin_guild_id="111", admin_role_id="222")
    assert s.admin_guild_id == 111
    assert s.admin_role_id == 222


def test_fallback_proxy_defaults_empty():
    s = Settings(_env_file=None, **_BASE)
    assert s.fallback_base_url == ""
    assert s.fallback_api_key == ""


def test_fallback_proxy_fields_set_when_provided():
    s = Settings(
        _env_file=None,
        **_BASE,
        fallback_base_url="http://127.0.0.1:8787",
        fallback_api_key="proxy-token",
    )
    assert s.fallback_base_url == "http://127.0.0.1:8787"
    assert s.fallback_api_key == "proxy-token"


def test_prefer_proxy_defaults():
    s = Settings(_env_file=None, **_BASE)
    assert s.prefer_proxy is False
    assert s.proxy_connect_timeout == 2.0
    assert s.proxy_breaker_cooldown == 30.0


def test_prefer_proxy_fields_set_when_provided():
    s = Settings(
        _env_file=None,
        **_BASE,
        prefer_proxy=True,
        proxy_connect_timeout=1.5,
        proxy_breaker_cooldown=45,
    )
    assert s.prefer_proxy is True
    assert s.proxy_connect_timeout == 1.5
    assert s.proxy_breaker_cooldown == 45.0
