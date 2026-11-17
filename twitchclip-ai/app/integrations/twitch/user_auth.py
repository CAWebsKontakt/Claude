"""OAuth 2.0 Authorization Code Flow für Streamer und Channel-Editoren (Modus B).

Ablauf: Autorisierungslink → Twitch-Login und Zustimmung → Callback mit `code` → Token-Tausch →
Validierung (`/oauth2/validate`) → verschlüsselte Speicherung. Tokens werden vor Ablauf erneuert und
stündlich validiert (Vorgabe von Twitch für Apps mit Nutzer-Tokens).
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.integrations.twitch.errors import TwitchAPIError, TwitchAuthError

# Für Create Clip From VOD und Get Clips Download genügt einer der beiden Scopes:
# channel:manage:clips (Broadcaster) bzw. editor:manage:clips (Channel-Editor).
CLIP_SCOPES = ("channel:manage:clips", "editor:manage:clips")
# Optional für Chat-Statistiken ab Phase 4 (EventSub channel.chat.message mit dem Broadcaster als Leser)
CHAT_SCOPES = ("user:read:chat", "user:bot", "channel:bot")


@dataclass(frozen=True)
class TokenGrant:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_in: int
    scopes: list[str]


@dataclass(frozen=True)
class TokenValidation:
    user_id: str
    login: str
    scopes: list[str]
    expires_in: int
    client_id: str


class TokenRefreshRejectedError(TwitchAuthError):
    """Refresh-Token ungültig (Zugriff widerrufen, Passwort geändert …) – Nutzer muss neu autorisieren."""


def requested_scopes(include_chat: bool) -> list[str]:
    return list(CLIP_SCOPES) + (list(CHAT_SCOPES) if include_chat else [])


def build_authorize_url(settings: Settings, state: str, scopes: list[str]) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.twitch_client_id,
            "redirect_uri": settings.twitch_redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            # Immer die Zustimmungsseite zeigen, damit bewusst das richtige Konto gewählt wird
            "force_verify": "true",
        }
    )
    return f"{settings.twitch_auth_base}/authorize?{query}"


def _grant_from(payload: dict) -> TokenGrant:
    if not payload.get("access_token"):
        raise TwitchAuthError("Token-Antwort ohne access_token")
    scopes = payload.get("scope") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return TokenGrant(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_in=int(payload.get("expires_in") or 0),
        scopes=list(scopes),
    )


def exchange_code(settings: Settings, http: httpx.Client, code: str) -> TokenGrant:
    try:
        response = http.post(
            f"{settings.twitch_auth_base}/token",
            data={
                "client_id": settings.twitch_client_id,
                "client_secret": settings.twitch_client_secret.get_secret_value(),
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": settings.twitch_redirect_uri,
            },
            timeout=15,
        )
    except httpx.HTTPError as exc:
        raise TwitchAPIError(
            f"Twitch-Token-Endpunkt nicht erreichbar: {exc.__class__.__name__}", retryable=True
        ) from exc
    if response.status_code != 200:
        raise TwitchAuthError(f"Code-Tausch abgelehnt (HTTP {response.status_code}): {response.text[:200]}")
    return _grant_from(response.json())


def refresh_grant(settings: Settings, http: httpx.Client, refresh_token: str) -> TokenGrant:
    try:
        response = http.post(
            f"{settings.twitch_auth_base}/token",
            data={
                "client_id": settings.twitch_client_id,
                "client_secret": settings.twitch_client_secret.get_secret_value(),
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=15,
        )
    except httpx.HTTPError as exc:
        raise TwitchAPIError(
            f"Twitch-Token-Endpunkt nicht erreichbar: {exc.__class__.__name__}", retryable=True
        ) from exc
    if response.status_code in (400, 401):
        raise TokenRefreshRejectedError(f"Refresh-Token abgelehnt: {response.text[:200]}")
    if response.status_code != 200:
        raise TwitchAPIError(f"Token-Erneuerung fehlgeschlagen (HTTP {response.status_code})", retryable=True)
    return _grant_from(response.json())


def validate_token(settings: Settings, http: httpx.Client, access_token: str) -> TokenValidation | None:
    """None = Token ungültig (401). Wirft bei Netzwerk-/Serverfehlern."""
    try:
        response = http.get(
            f"{settings.twitch_auth_base}/validate",
            headers={"Authorization": f"OAuth {access_token}"},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        raise TwitchAPIError(
            f"Token-Validierung nicht erreichbar: {exc.__class__.__name__}", retryable=True
        ) from exc
    if response.status_code == 401:
        return None
    if response.status_code != 200:
        raise TwitchAPIError(f"Token-Validierung: HTTP {response.status_code}", retryable=True)
    data = response.json()
    return TokenValidation(
        user_id=str(data.get("user_id", "")),
        login=str(data.get("login", "")),
        scopes=list(data.get("scopes") or []),
        expires_in=int(data.get("expires_in") or 0),
        client_id=str(data.get("client_id", "")),
    )


def revoke_token(settings: Settings, http: httpx.Client, access_token: str) -> None:
    """Widerruft ein Token bei Twitch (beste Mühe – lokal wird es in jedem Fall gesperrt)."""
    with suppress(httpx.HTTPError):
        http.post(
            f"{settings.twitch_auth_base}/revoke",
            data={"client_id": settings.twitch_client_id, "token": access_token},
            timeout=10,
        )
