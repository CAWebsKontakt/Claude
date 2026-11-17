import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.config import Settings


def make(**overrides):
    base = {"_env_file": None, "app_env": "test"}
    base.update(overrides)
    return Settings(**base)


def test_streamer_list_parses_comma_separated_and_normalizes():
    s = make(streamer_list=" @KanalEins, kanalzwei;kanaldrei ,, kanaleins")
    assert s.streamer_list == ["kanaleins", "kanalzwei", "kanaldrei"]


def test_streamer_list_from_environment(monkeypatch):
    monkeypatch.setenv("STREAMER_LIST", "alpha,beta")
    s = make()
    assert s.streamer_list == ["alpha", "beta"]


def test_defaults_match_specification():
    s = make()
    assert s.timezone == "Europe/Berlin"
    assert (s.clip_duration_min, s.clip_duration_max) == (20, 45)
    assert (s.highlights_per_stream_min, s.highlights_per_stream_max) == (3, 5)
    assert s.tiktok_direct_post_enabled is False


def test_duration_min_greater_than_max_rejected():
    with pytest.raises(ValidationError, match="CLIP_DURATION_MIN"):
        make(clip_duration_min=50, clip_duration_max=30)


@pytest.mark.parametrize("value", [-1, 101])
def test_min_highlight_score_range(value):
    with pytest.raises(ValidationError):
        make(min_highlight_score=value)


def test_invalid_timezone_rejected():
    with pytest.raises(ValidationError, match="Zeitzone"):
        make(timezone="Mars/Olympus")


def test_invalid_language_rejected():
    with pytest.raises(ValidationError):
        make(clip_language="fr")


def test_production_requires_strong_secrets():
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        make(app_env="production", secret_key="short", app_base_url="https://x.example")
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        make(app_env="production", secret_key="CHANGE_ME" * 5, app_base_url="https://x.example")
    with pytest.raises(ValidationError, match="TOKEN_ENCRYPTION_KEY"):
        make(
            app_env="production",
            secret_key="x" * 40,
            token_encryption_key="not-a-key",
            app_base_url="https://x.example",
        )
    with pytest.raises(ValidationError, match="https"):
        make(
            app_env="production",
            secret_key="x" * 40,
            token_encryption_key=Fernet.generate_key().decode(),
            app_base_url="http://x.example",
        )


def test_production_valid_configuration():
    key = Fernet.generate_key().decode()
    s = make(
        app_env="production",
        secret_key="x" * 40,
        token_encryption_key=f"{key},{Fernet.generate_key().decode()}",
        app_base_url="https://clips.example.com/",
    )
    assert s.app_base_url == "https://clips.example.com"
    assert len(s.encryption_keys) == 2
    assert s.eventsub_callback_url == "https://clips.example.com/webhooks/twitch/eventsub"
    assert s.twitch_redirect_uri == "https://clips.example.com/oauth/twitch/callback"


def test_public_summary_never_contains_secrets():
    s = make(
        secret_key="super-secret-value-1234567890-abcdef",
        twitch_client_secret="tw-secret",
        tiktok_client_secret="tt-secret",
    )
    text = str(s.public_summary())
    for secret in ("super-secret-value", "tw-secret", "tt-secret"):
        assert secret not in text
