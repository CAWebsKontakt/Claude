"""Fehlerklassen der TikTok-Anbindung mit verständlichen Erklärungen bekannter Fehlercodes."""

from __future__ import annotations

# Bekannte Fehlercodes der Content Posting API → Erklärung für das Dashboard
KNOWN_ERRORS = {
    "unaudited_client_can_only_post_to_private_accounts": (
        "Die App ist von TikTok noch nicht für öffentliche Posts auditiert: Es sind nur private Posts "
        "(„Nur ich“) möglich, und das TikTok-Konto muss privat sein. "
        "Alternativ Inbox-Upload oder Export nutzen."
    ),
    "spam_risk_too_many_posts": (
        "Tageslimit für Posts dieses TikTok-Kontos erreicht – morgen erneut versuchen."
    ),
    "spam_risk_user_banned_from_posting": "TikTok hat dieses Konto vorübergehend für Posts gesperrt.",
    "reached_active_user_cap": "Tageslimit aktiver Nutzer dieser (nicht auditierten) App erreicht.",
    "privacy_level_option_mismatch": "Die gewählte Sichtbarkeit ist für dieses Konto nicht (mehr) erlaubt.",
    "rate_limit_exceeded": "Zu viele Anfragen an TikTok – wird automatisch später erneut versucht.",
    "access_token_invalid": (
        "Die TikTok-Verbindung ist abgelaufen oder wurde widerrufen – Konto neu verbinden."
    ),
    "scope_not_authorized": "Dem Konto fehlt die nötige Berechtigung (Scope) – Konto neu verbinden.",
    "duration_check_failed": "Das Video ist länger als für dieses Konto erlaubt.",
    "file_format_check_failed": "TikTok akzeptiert die Videodatei nicht (Format).",
    "picture_size_check_failed": "TikTok akzeptiert die Bildgröße nicht.",
    "frame_rate_check_failed": "TikTok akzeptiert die Bildrate nicht.",
    "video_pull_failed": "TikTok konnte das Video nicht laden.",
}


def explain(code: str | None, message: str | None = None) -> str:
    if code and code in KNOWN_ERRORS:
        return KNOWN_ERRORS[code]
    return f"TikTok-Fehler {code or 'unbekannt'}: {message or 'ohne Beschreibung'}"


class TikTokError(Exception):
    """Basisklasse."""


class TikTokNotConfiguredError(TikTokError):
    pass


class TikTokAuthError(TikTokError):
    """Token ungültig, abgelaufen oder Refresh abgelehnt."""


class TikTokAPIError(TikTokError):
    def __init__(
        self,
        code: str | None,
        message: str | None = None,
        *,
        status_code: int | None = None,
        log_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(explain(code, message))
        self.code = code
        self.raw_message = message
        self.status_code = status_code
        self.log_id = log_id
        self.retryable = retryable


class TikTokRateLimitedError(TikTokAPIError):
    def __init__(self, retry_after: float = 60.0, log_id: str | None = None) -> None:
        super().__init__("rate_limit_exceeded", None, status_code=429, log_id=log_id, retryable=True)
        self.retry_after = retry_after
