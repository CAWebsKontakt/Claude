import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from app.config import reset_settings_cache
from app.models import OAuthToken
from app.security import crypto
from app.security.passwords import PasswordPolicyError, hash_password, validate_password, verify_password
from app.security.ratelimit import LoginRateLimiter


def test_encrypt_roundtrip(settings):
    token = crypto.encrypt("geheimes-token")
    assert token != "geheimes-token"
    assert crypto.decrypt(token) == "geheimes-token"


def test_key_rotation(settings, monkeypatch):
    old_value = crypto.encrypt("rotier-mich")
    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", f"{new_key},{settings.encryption_keys[0]}")
    reset_settings_cache()
    crypto._cipher_for.cache_clear()
    assert crypto.decrypt(old_value) == "rotier-mich"  # alter Schlüssel entschlüsselt noch
    rotated = crypto.rotate(old_value)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", new_key)
    reset_settings_cache()
    crypto._cipher_for.cache_clear()
    assert crypto.decrypt(rotated) == "rotier-mich"
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(old_value)


def test_missing_key_raises(settings, monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "")
    reset_settings_cache()
    crypto._cipher_for.cache_clear()
    with pytest.raises(crypto.EncryptionNotConfiguredError):
        crypto.encrypt("x")


def test_oauth_tokens_are_stored_encrypted(db_session):
    db_session.add(
        OAuthToken(
            provider="twitch",
            external_user_id="123",
            scopes=["channel:manage:clips"],
            access_token="klartext-access",
            refresh_token="klartext-refresh",
        )
    )
    db_session.commit()
    raw = db_session.execute(text("SELECT access_token, refresh_token FROM oauth_tokens")).one()
    assert "klartext" not in raw[0] and "klartext" not in raw[1]
    db_session.expire_all()
    token = db_session.query(OAuthToken).one()
    assert token.access_token == "klartext-access"
    assert token.refresh_token == "klartext-refresh"


def test_password_hash_and_verify():
    hashed = hash_password("Ein-Langes-Passwort-1")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "Ein-Langes-Passwort-1")
    assert not verify_password(hashed, "falsch")
    assert not verify_password(None, "egal")
    assert not verify_password("kein-hash", "egal")


@pytest.mark.parametrize("password", ["kurz", "aaaaaaaaaaaaaaaa", "admin-passwort-123"])
def test_password_policy(password):
    with pytest.raises(PasswordPolicyError):
        validate_password(password, username="admin")


def test_login_rate_limiter(redis):
    limiter = LoginRateLimiter(redis, max_attempts=3, window_seconds=60)
    for _ in range(3):
        assert not limiter.is_blocked("1.2.3.4", "alice")
        limiter.register_failure("1.2.3.4", "alice")
    assert limiter.is_blocked("1.2.3.4", "alice")
    assert not limiter.is_blocked("5.6.7.8", "bob")
    limiter.reset("1.2.3.4", "alice")
    assert not limiter.is_blocked("1.2.3.4", "alice")
