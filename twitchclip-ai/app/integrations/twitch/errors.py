"""Fehlerklassen der Twitch-Anbindung."""

from __future__ import annotations


class TwitchError(Exception):
    """Basisklasse."""


class TwitchNotConfiguredError(TwitchError):
    """TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET fehlen."""


class TwitchAuthError(TwitchError):
    """Token konnte nicht bezogen werden oder wurde abgelehnt (401)."""


class TwitchAPIError(TwitchError):
    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class TwitchRateLimitedError(TwitchAPIError):
    """Rate-Limit erreicht. retry_after = Sekunden bis zum Zurücksetzen des Kontingents."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(
            f"Twitch-Rate-Limit erreicht, erneut in {retry_after:.0f} s", status_code=429, retryable=True
        )
        self.retry_after = retry_after


class EventSubConflictError(TwitchAPIError):
    """Das EventSub-Abo existiert bereits (HTTP 409)."""
