"""Verschlüsselung von OAuth-Tokens und anderen Geheimnissen in der Datenbank (Fernet/AES-128-CBC + HMAC).

TOKEN_ENCRYPTION_KEY darf mehrere kommagetrennte Schlüssel enthalten. Der erste Schlüssel
verschlüsselt neue Werte, alle Schlüssel können entschlüsseln. So lassen sich Schlüssel
rotieren: neuen Schlüssel voranstellen, `python -m app.cli rotate-token-key` ausführen,
alten Schlüssel danach entfernen.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.config import get_settings


class EncryptionNotConfiguredError(RuntimeError):
    pass


class DecryptionError(RuntimeError):
    pass


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


@lru_cache(maxsize=8)
def _cipher_for(keys: tuple[str, ...]) -> MultiFernet:
    if not keys:
        raise EncryptionNotConfiguredError(
            "TOKEN_ENCRYPTION_KEY ist nicht gesetzt – OAuth-Tokens können nicht sicher gespeichert werden."
        )
    try:
        return MultiFernet([Fernet(k.encode("ascii")) for k in keys])
    except (ValueError, TypeError) as exc:
        raise EncryptionNotConfiguredError("TOKEN_ENCRYPTION_KEY enthält einen ungültigen Schlüssel") from exc


def get_cipher() -> MultiFernet:
    return _cipher_for(tuple(get_settings().encryption_keys))


def encrypt(plaintext: str) -> str:
    return get_cipher().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    try:
        return get_cipher().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise DecryptionError("Wert konnte nicht entschlüsselt werden (falscher Schlüssel?)") from exc


def rotate(token: str) -> str:
    """Verschlüsselt einen Wert mit dem aktuell ersten Schlüssel neu."""
    try:
        return get_cipher().rotate(token.encode("ascii")).decode("ascii")
    except InvalidToken as exc:
        raise DecryptionError("Wert konnte nicht rotiert werden") from exc


class EncryptedText(TypeDecorator[str]):
    """SQLAlchemy-Spaltentyp: speichert Text ausschließlich verschlüsselt."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: object) -> str | None:
        if value is None:
            return None
        return encrypt(value)

    def process_result_value(self, value: str | None, dialect: object) -> str | None:
        if value is None:
            return None
        return decrypt(value)
