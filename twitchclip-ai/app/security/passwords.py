"""Passwort-Hashing mit Argon2id und eine einfache Passwort-Richtlinie."""

from __future__ import annotations

from contextlib import suppress

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256

_hasher = PasswordHasher()  # Argon2id, Parameter nach RFC 9106 (low-memory profile)
# Wird bei unbekannten Benutzern geprüft, damit die Antwortzeit keine Benutzernamen verrät.
_DUMMY_HASH = _hasher.hash("timing-equalizer-not-a-real-password")


class PasswordPolicyError(ValueError):
    pass


def validate_password(password: str, *, username: str | None = None) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Das Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen lang sein.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError("Das Passwort ist zu lang.")
    if len(set(password)) < 4:
        raise PasswordPolicyError("Das Passwort ist zu einfach.")
    if username and username.lower() in password.lower():
        raise PasswordPolicyError("Das Passwort darf den Benutzernamen nicht enthalten.")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash:
        with suppress(VerificationError):
            _hasher.verify(_DUMMY_HASH, password)
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True
