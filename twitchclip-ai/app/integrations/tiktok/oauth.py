"""TikTok Login Kit (OAuth 2.0 v2) mit PKCE.

Autorisierung: https://www.tiktok.com/v2/auth/authorize/  ·  Token: POST /v2/oauth/token/
Access-Token ca. 24 h, Refresh-Token ca. 365 Tage. Widerruf: POST /v2/oauth/revoke/.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.integrations.tiktok.errors import TikTokAPIError, TikTokAuthError, TikTokNotConfiguredError


@dataclass(frozen=True)
class TikTokGrant:
    open_id: str
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_in: int
    refresh_expires_in: int
    scopes: list[str]


def _require(settings: Settings) -> None:
    if not settings.tiktok_configured:
        raise TikTokNotConfiguredError("TIKTOK_CLIENT_KEY/TIKTOK_CLIENT_SECRET sind nicht gesetzt")


def new_pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge) nach RFC 7636, Methode S256."""
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def scopes(settings: Settings) -> list[str]:
    return [s.strip() for s in settings.tiktok_scopes.split(",") if s.strip()]


def authorize_url(settings: Settings, state: str, code_challenge: str | None) -> str:
    _require(settings)
    params = {
        "client_key": settings.tiktok_client_key,
        "scope": ",".join(scopes(settings)),
        "response_type": "code",
        "redirect_uri": settings.tiktok_redirect_uri,
        "state": state,
    }
    if code_challenge:
        params.update(code_challenge=code_challenge, code_challenge_method="S256")
    return f"{settings.tiktok_auth_url}?{urlencode(params)}"


def _token_request(settings: Settings, http: httpx.Client, data: dict[str, str]) -> TikTokGrant:
    _require(settings)
    payload = {
        "client_key": settings.tiktok_client_key,
        "client_secret": settings.tiktok_client_secret.get_secret_value(),
        **data,
    }
    try:
        response = http.post(
            f"{settings.tiktok_api_base}/v2/oauth/token/",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise TikTokAPIError("network", str(exc.__class__.__name__), retryable=True) from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or "access_token" not in body:
        error = body.get("error") or "token_error"
        if isinstance(error, dict):
            error = error.get("code") or "token_error"
        description = body.get("error_description") or body.get("message") or response.text[:200]
        raise TikTokAuthError(f"TikTok lehnt die Anmeldung ab ({error}): {description}")
    raw_scopes = body.get("scope") or ""
    return TikTokGrant(
        open_id=str(body.get("open_id", "")),
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_in=int(body.get("expires_in") or 86400),
        refresh_expires_in=int(body.get("refresh_expires_in") or 0),
        scopes=[s.strip() for s in str(raw_scopes).replace(" ", ",").split(",") if s.strip()],
    )


def exchange_code(
    settings: Settings, http: httpx.Client, code: str, code_verifier: str | None
) -> TikTokGrant:
    data = {"code": code, "grant_type": "authorization_code", "redirect_uri": settings.tiktok_redirect_uri}
    if code_verifier:
        data["code_verifier"] = code_verifier
    return _token_request(settings, http, data)


def refresh(settings: Settings, http: httpx.Client, refresh_token: str) -> TikTokGrant:
    return _token_request(settings, http, {"grant_type": "refresh_token", "refresh_token": refresh_token})


def revoke(settings: Settings, http: httpx.Client, access_token: str) -> None:
    with suppress(httpx.HTTPError, TikTokNotConfiguredError):
        _require(settings)
        http.post(
            f"{settings.tiktok_api_base}/v2/oauth/revoke/",
            data={
                "client_key": settings.tiktok_client_key,
                "client_secret": settings.tiktok_client_secret.get_secret_value(),
                "token": access_token,
            },
            timeout=15,
        )
