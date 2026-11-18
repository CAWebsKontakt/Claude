"""TikTok-Konten verbinden, Tokens erneuern, trennen, Creator-Info abfragen."""

from __future__ import annotations

import json
import logging
import secrets
from datetime import timedelta

import httpx
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import ActivityCategory, ActivityLevel, OAuthProvider
from app.integrations.tiktok import oauth
from app.integrations.tiktok.client import CreatorInfo, TikTokClient
from app.integrations.tiktok.errors import TikTokAPIError, TikTokAuthError
from app.models import OAuthToken, TikTokAccount, User
from app.services.activity import log_activity
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

STATE_PREFIX = "tcai:tiktok:oauth-state:"
STATE_TTL = 15 * 60
REFRESH_MARGIN = timedelta(minutes=10)


class TikTokAccountError(Exception):
    """Fehler mit einer Meldung für das Dashboard."""


def begin_connect(settings: Settings, redis: Redis, user: User) -> str:
    if not settings.tiktok_configured:
        raise TikTokAccountError("TikTok ist nicht konfiguriert (TIKTOK_CLIENT_KEY/TIKTOK_CLIENT_SECRET).")
    state = secrets.token_urlsafe(24)
    verifier, challenge = oauth.new_pkce() if settings.tiktok_use_pkce else (None, None)
    redis.set(STATE_PREFIX + state, json.dumps({"u": user.id, "v": verifier}), ex=STATE_TTL)
    return oauth.authorize_url(settings, state, challenge)


def _pop_state(redis: Redis, state: str) -> dict | None:
    pipe = redis.pipeline()
    pipe.get(STATE_PREFIX + state)
    pipe.delete(STATE_PREFIX + state)
    raw, _ = pipe.execute()
    return json.loads(raw) if raw else None


def _fetch_profile(settings: Settings, http: httpx.Client, access_token: str) -> dict:
    try:
        response = http.get(
            f"{settings.tiktok_api_base}/v2/user/info/",
            params={"fields": "open_id,union_id,avatar_url,display_name"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        return (response.json().get("data") or {}).get("user") or {}
    except (httpx.HTTPError, ValueError):
        return {}


def complete_connect(
    db: Session, settings: Settings, redis: Redis, http: httpx.Client, *, state: str, code: str, user: User
) -> TikTokAccount:
    data = _pop_state(redis, state) if state else None
    if data is None or data.get("u") != user.id:
        raise TikTokAccountError(
            "Die Anmeldung ist abgelaufen oder gehört zu einer anderen Sitzung. Bitte erneut starten."
        )
    grant = oauth.exchange_code(settings, http, code, data.get("v"))
    if not grant.open_id:
        raise TikTokAccountError("TikTok hat keine Konto-ID geliefert.")
    profile = _fetch_profile(settings, http, grant.access_token)

    account = db.scalar(select(TikTokAccount).where(TikTokAccount.open_id == grant.open_id))
    if account is None:
        account = TikTokAccount(open_id=grant.open_id)
        db.add(account)
    account.union_id = profile.get("union_id") or account.union_id
    account.display_name = profile.get("display_name") or account.display_name
    account.avatar_url = profile.get("avatar_url") or account.avatar_url
    account.is_active = True
    account.connected_by_user_id = user.id
    db.flush()

    token = db.scalar(
        select(OAuthToken).where(
            OAuthToken.provider == OAuthProvider.TIKTOK.value, OAuthToken.tiktok_account_id == account.id
        )
    )
    if token is None:
        token = OAuthToken(
            provider=OAuthProvider.TIKTOK.value, tiktok_account_id=account.id, external_user_id=grant.open_id
        )
        db.add(token)
    _apply_grant(token, grant)
    token.external_login = account.display_name
    db.flush()
    try:
        refresh_creator_info(db, settings, http, account)
    except (TikTokAPIError, TikTokAuthError) as exc:
        logger.warning("Creator-Info nach dem Verbinden nicht abrufbar", extra={"error": str(exc)})
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.TIKTOK,
        f"TikTok-Konto „{account.display_name or account.open_id}“ verbunden "
        f"(Scopes: {', '.join(token.scopes)})",
        user_id=user.id,
    )
    return account


def _apply_grant(token: OAuthToken, grant: oauth.TikTokGrant) -> None:
    now = utcnow()
    token.access_token = grant.access_token
    if grant.refresh_token:
        token.refresh_token = grant.refresh_token
    token.expires_at = now + timedelta(seconds=grant.expires_in)
    if grant.refresh_expires_in:
        token.refresh_expires_at = now + timedelta(seconds=grant.refresh_expires_in)
    if grant.scopes:
        token.scopes = sorted(grant.scopes)
    token.revoked_at = None
    token.last_refresh_error = None


def active_token(account: TikTokAccount) -> OAuthToken | None:
    for token in account.oauth_tokens:
        if token.provider == OAuthProvider.TIKTOK.value and token.revoked_at is None:
            return token
    return None


def _mark_broken(db: Session, account: TikTokAccount, token: OAuthToken, reason: str) -> None:
    token.revoked_at = utcnow()
    token.last_refresh_error = reason[:1000]
    db.flush()
    log_activity(
        db,
        ActivityLevel.WARNING,
        ActivityCategory.TIKTOK,
        f"TikTok-Konto „{account.display_name or account.open_id}“ muss neu verbunden werden: {reason}",
    )


def access_token_for(
    db: Session,
    settings: Settings,
    http: httpx.Client,
    account: TikTokAccount,
    *,
    force_refresh: bool = False,
) -> str:
    token = active_token(account)
    if token is None or not account.is_active:
        raise TikTokAuthError("Das TikTok-Konto ist nicht (mehr) verbunden.")
    if force_refresh or token.expires_at is None or token.expires_at - utcnow() < REFRESH_MARGIN:
        if not token.refresh_token:
            _mark_broken(db, account, token, "kein Refresh-Token vorhanden")
            raise TikTokAuthError("Kein Refresh-Token")
        try:
            grant = oauth.refresh(settings, http, token.refresh_token)
        except TikTokAuthError as exc:
            _mark_broken(db, account, token, f"Erneuerung abgelehnt ({exc})")
            raise
        _apply_grant(token, grant)
        db.flush()
    return token.access_token


def client_for(db: Session, settings: Settings, http: httpx.Client, account: TikTokAccount) -> TikTokClient:
    return TikTokClient(settings, http, access_token_for(db, settings, http, account))


def refresh_creator_info(
    db: Session, settings: Settings, http: httpx.Client, account: TikTokAccount
) -> CreatorInfo:
    """Creator-Info live abfragen (Pflicht vor jeder Veröffentlichung) und zur Anzeige zwischenspeichern."""
    client = client_for(db, settings, http, account)
    try:
        info = client.creator_info()
    except TikTokAuthError:
        client = TikTokClient(
            settings, http, access_token_for(db, settings, http, account, force_refresh=True)
        )
        info = client.creator_info()
    account.creator_info = info.as_dict()
    account.creator_info_fetched_at = utcnow()
    if info.username:
        account.username = info.username
    if info.nickname:
        account.display_name = info.nickname
    if info.avatar_url:
        account.avatar_url = info.avatar_url
    db.flush()
    return info


def refresh_expiring_tokens(db: Session, settings: Settings, http: httpx.Client) -> dict:
    """Stündlich: Access-Tokens rechtzeitig erneuern, damit Konten dauerhaft verbunden bleiben."""
    result = {"refreshed": 0, "failed": 0}
    soon = utcnow() + timedelta(hours=2)
    for account in db.scalars(select(TikTokAccount).where(TikTokAccount.is_active.is_(True))).all():
        token = active_token(account)
        if token is None or (token.expires_at and token.expires_at > soon):
            continue
        try:
            access_token_for(db, settings, http, account, force_refresh=True)
            result["refreshed"] += 1
        except (TikTokAuthError, TikTokAPIError):
            result["failed"] += 1
    return result


def disconnect(
    db: Session, settings: Settings, http: httpx.Client, account: TikTokAccount, *, actor: User
) -> None:
    token = active_token(account)
    if token is not None:
        oauth.revoke(settings, http, token.access_token)
        token.revoked_at = utcnow()
        token.last_refresh_error = f"im Dashboard getrennt von {actor.username}"
    account.is_active = False
    db.flush()
    log_activity(
        db,
        ActivityLevel.WARNING,
        ActivityCategory.TIKTOK,
        f"TikTok-Konto „{account.display_name or account.open_id}“ getrennt",
        user_id=actor.id,
    )
