"""Twitch-Autorisierung von Streamern und Channel-Editoren (Voraussetzung für Modus B).

1. Ein Redakteur erzeugt im Dashboard einen signierten Einladungslink (nur mit dokumentierter Erlaubnis).
2. Der Streamer bzw. Editor öffnet den Link, sieht genau, welche Rechte wozu angefragt werden, und
   meldet sich bei Twitch an. Er braucht dafür kein Dashboard-Konto.
3. Nach dem Callback wird das Token geprüft, die Rolle bestimmt (Broadcaster oder Editor) und das Token
   verschlüsselt gespeichert. Widerruf jederzeit über Twitch oder das Dashboard.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import httpx
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import ActivityCategory, ActivityLevel, OAuthProvider, StreamerMode, TwitchAuthRole
from app.integrations.twitch.client import UserCredential
from app.integrations.twitch.errors import TwitchAuthError
from app.integrations.twitch.user_auth import (
    CHAT_SCOPES,
    CLIP_SCOPES,
    TokenRefreshRejectedError,
    build_authorize_url,
    exchange_code,
    refresh_grant,
    requested_scopes,
    revoke_token,
    validate_token,
)
from app.models import OAuthToken, Streamer, User
from app.services.activity import log_activity
from app.services.streamers import active_permission
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

INVITE_SALT = "twitch-invite-v1"
STATE_PREFIX = "tcai:twitch:oauth-state:"
USED_INVITE_PREFIX = "tcai:twitch:invite-used:"
STATE_TTL_SECONDS = 15 * 60
REFRESH_MARGIN = timedelta(minutes=5)

ROLE_LABELS = {TwitchAuthRole.BROADCASTER: "Streamer selbst", TwitchAuthRole.EDITOR: "Channel-Editor"}


class AuthorizationError(Exception):
    """Fehler mit einer Meldung, die dem Streamer/Editor angezeigt werden darf."""


@dataclass(frozen=True)
class InviteData:
    streamer_id: int
    nonce: str
    include_chat: bool


@dataclass(frozen=True)
class AuthorizationResult:
    streamer_name: str
    login: str
    role: str
    scopes: list[str]
    chat_enabled: bool


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key.get_secret_value(), salt=INVITE_SALT)


# ---------------------------------------------------------------------------
# Einladung
# ---------------------------------------------------------------------------
def create_invite(
    db: Session, settings: Settings, streamer: Streamer, *, actor: User, include_chat: bool
) -> str:
    if not settings.twitch_configured:
        raise AuthorizationError("Die Twitch-API ist nicht konfiguriert.")
    if streamer.mode != StreamerMode.AUTHORIZED or active_permission(streamer) is None:
        raise AuthorizationError("Zuerst eine gültige Erlaubnis dokumentieren und Modus B aktivieren.")
    if not streamer.twitch_user_id:
        raise AuthorizationError("Das Twitch-Konto ist noch nicht verknüpft – bitte 'Twitch jetzt abfragen'.")
    payload = {"s": streamer.id, "n": secrets.token_urlsafe(12), "c": bool(include_chat)}
    token = _serializer(settings).dumps(payload)
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.PERMISSION,
        f"Autorisierungslink für '{streamer.twitch_login}' erstellt "
        f"(gültig {settings.twitch_invite_valid_days} Tage"
        f"{', inkl. Chat-Statistik' if include_chat else ''})",
        user_id=actor.id,
        streamer_id=streamer.id,
    )
    return f"{settings.app_base_url}/oauth/twitch/start?invite={token}"


def read_invite(settings: Settings, redis: Redis, token: str) -> InviteData:
    try:
        data = _serializer(settings).loads(token, max_age=settings.twitch_invite_valid_days * 86400)
    except SignatureExpired as exc:
        raise AuthorizationError("Dieser Link ist abgelaufen. Bitte einen neuen Link anfordern.") from exc
    except BadSignature as exc:
        raise AuthorizationError("Dieser Link ist ungültig.") from exc
    invite = InviteData(streamer_id=int(data["s"]), nonce=str(data["n"]), include_chat=bool(data.get("c")))
    if redis.exists(USED_INVITE_PREFIX + invite.nonce):
        raise AuthorizationError("Dieser Link wurde bereits verwendet. Bitte einen neuen Link anfordern.")
    return invite


def begin_authorization(settings: Settings, redis: Redis, invite: InviteData) -> str:
    """Legt einen einmaligen OAuth-State an und liefert die Twitch-Anmeldeadresse."""
    state = secrets.token_urlsafe(24)
    redis.set(
        STATE_PREFIX + state,
        json.dumps({"s": invite.streamer_id, "n": invite.nonce, "c": invite.include_chat}),
        ex=STATE_TTL_SECONDS,
    )
    return build_authorize_url(settings, state, requested_scopes(invite.include_chat))


def _pop_state(redis: Redis, state: str) -> dict | None:
    pipe = redis.pipeline()
    pipe.get(STATE_PREFIX + state)
    pipe.delete(STATE_PREFIX + state)
    raw, _deleted = pipe.execute()
    return json.loads(raw) if raw else None


def complete_authorization(
    db: Session, settings: Settings, redis: Redis, http: httpx.Client, *, state: str, code: str
) -> AuthorizationResult:
    data = _pop_state(redis, state) if state else None
    if data is None:
        raise AuthorizationError("Die Anmeldung ist abgelaufen oder ungültig. Bitte den Link erneut öffnen.")
    if redis.exists(USED_INVITE_PREFIX + data["n"]):
        raise AuthorizationError("Dieser Link wurde bereits verwendet.")
    streamer = db.get(Streamer, int(data["s"]))
    if streamer is None or not streamer.twitch_user_id:
        raise AuthorizationError("Der Kanal ist nicht mehr vorhanden.")

    grant = exchange_code(settings, http, code)
    validation = validate_token(settings, http, grant.access_token)
    if validation is None:
        raise AuthorizationError("Twitch hat das Token nicht bestätigt. Bitte erneut versuchen.")
    if validation.client_id and validation.client_id != settings.twitch_client_id:
        raise AuthorizationError("Das Token gehört zu einer anderen Anwendung.")
    scopes = validation.scopes or grant.scopes
    if not set(scopes) & set(CLIP_SCOPES):
        revoke_token(settings, http, grant.access_token)
        raise AuthorizationError(
            "Die Berechtigung für Clips wurde nicht erteilt – ohne sie ist keine Verarbeitung möglich."
        )

    role = (
        TwitchAuthRole.BROADCASTER if validation.user_id == streamer.twitch_user_id else TwitchAuthRole.EDITOR
    )
    token = db.scalar(
        select(OAuthToken).where(
            OAuthToken.provider == OAuthProvider.TWITCH.value,
            OAuthToken.streamer_id == streamer.id,
            OAuthToken.external_user_id == validation.user_id,
        )
    )
    if token is None:
        token = OAuthToken(
            provider=OAuthProvider.TWITCH.value, streamer_id=streamer.id, external_user_id=validation.user_id
        )
        db.add(token)
    now = utcnow()
    token.external_login = validation.login
    token.scopes = sorted(scopes)
    token.access_token = grant.access_token
    token.refresh_token = grant.refresh_token
    token.expires_at = now + timedelta(seconds=grant.expires_in or validation.expires_in or 3600)
    token.revoked_at = None
    token.last_refresh_error = None
    token.last_validated_at = now
    token.role = role.value
    db.flush()
    redis.set(USED_INVITE_PREFIX + data["n"], "1", ex=(settings.twitch_invite_valid_days + 1) * 86400)

    chat_enabled = set(CHAT_SCOPES) <= set(scopes)
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.PERMISSION,
        f"Twitch-Autorisierung für '{streamer.twitch_login}' erteilt von {validation.login} "
        f"({ROLE_LABELS[role]}; Scopes: {', '.join(token.scopes)})",
        streamer_id=streamer.id,
    )
    if role == TwitchAuthRole.EDITOR:
        log_activity(
            db,
            ActivityLevel.INFO,
            ActivityCategory.PERMISSION,
            f"Hinweis: {validation.login} ist nicht der Kanalinhaber. Twitch prüft beim ersten Clip-Aufruf, "
            "ob das Konto Editor des Kanals ist.",
            streamer_id=streamer.id,
        )
    return AuthorizationResult(
        streamer_name=streamer.name,
        login=validation.login,
        role=role.value,
        scopes=token.scopes,
        chat_enabled=chat_enabled,
    )


# ---------------------------------------------------------------------------
# Token-Lebenszyklus
# ---------------------------------------------------------------------------
def mark_revoked(db: Session, token: OAuthToken, reason: str, *, actor: User | None = None) -> None:
    if token.revoked_at is not None:
        return
    token.revoked_at = utcnow()
    token.last_refresh_error = reason[:1000]
    db.flush()
    streamer = db.get(Streamer, token.streamer_id) if token.streamer_id else None
    log_activity(
        db,
        ActivityLevel.WARNING,
        ActivityCategory.PERMISSION,
        f"Twitch-Autorisierung von {token.external_login or token.external_user_id}"
        + (f" für '{streamer.twitch_login}'" if streamer else "")
        + f" beendet: {reason}",
        user_id=actor.id if actor else None,
        streamer_id=token.streamer_id,
    )


def refresh_token_row(db: Session, settings: Settings, http: httpx.Client, token: OAuthToken) -> None:
    if not token.refresh_token:
        mark_revoked(db, token, "kein Refresh-Token vorhanden")
        raise TwitchAuthError("Kein Refresh-Token")
    try:
        grant = refresh_grant(settings, http, token.refresh_token)
    except TokenRefreshRejectedError as exc:
        mark_revoked(db, token, "Erneuerung von Twitch abgelehnt (Zugriff widerrufen?)")
        raise TwitchAuthError(str(exc)) from exc
    token.access_token = grant.access_token
    if grant.refresh_token:
        token.refresh_token = grant.refresh_token
    token.expires_at = utcnow() + timedelta(seconds=grant.expires_in or 3600)
    if grant.scopes:
        token.scopes = sorted(grant.scopes)
    token.last_refresh_error = None
    db.flush()


def credential_for(db: Session, settings: Settings, http: httpx.Client, token: OAuthToken) -> UserCredential:
    if token.revoked_at is not None:
        raise TwitchAuthError("Autorisierung wurde widerrufen")
    if token.expires_at is None or token.expires_at - utcnow() < REFRESH_MARGIN:
        refresh_token_row(db, settings, http, token)
    return UserCredential(user_id=token.external_user_id, access_token=token.access_token)


def call_with_user_token[T](
    db: Session,
    settings: Settings,
    http: httpx.Client,
    token: OAuthToken,
    action: Callable[[UserCredential], T],
) -> T:
    """Führt einen Helix-Aufruf mit dem Nutzer-Token aus; bei 401 einmal erneuern und wiederholen."""
    credential = credential_for(db, settings, http, token)
    try:
        return action(credential)
    except TwitchAuthError:
        refresh_token_row(db, settings, http, token)
        credential = UserCredential(user_id=token.external_user_id, access_token=token.access_token)
        try:
            return action(credential)
        except TwitchAuthError as exc:
            mark_revoked(db, token, "Twitch lehnt das Token ab (401)")
            raise exc


@dataclass
class ValidationReport:
    valid: int = 0
    refreshed: int = 0
    revoked: int = 0


def validate_all_tokens(db: Session, settings: Settings, http: httpx.Client) -> ValidationReport:
    """Stündliche Prüfung aller aktiven Twitch-Nutzer-Tokens (Twitch-Vorgabe)."""
    report = ValidationReport()
    tokens = db.scalars(
        select(OAuthToken).where(
            OAuthToken.provider == OAuthProvider.TWITCH.value,
            OAuthToken.revoked_at.is_(None),
            OAuthToken.streamer_id.is_not(None),
        )
    ).all()
    for token in tokens:
        validation = validate_token(settings, http, token.access_token)
        if validation is None:
            try:
                refresh_token_row(db, settings, http, token)
            except TwitchAuthError:
                report.revoked += 1
                continue
            report.refreshed += 1
            validation = validate_token(settings, http, token.access_token)
            if validation is None:
                mark_revoked(db, token, "Token nach Erneuerung weiterhin ungültig")
                report.revoked += 1
                continue
        token.scopes = sorted(validation.scopes) or token.scopes
        token.last_validated_at = utcnow()
        if not set(token.scopes) & set(CLIP_SCOPES):
            mark_revoked(db, token, "Clip-Berechtigung nicht mehr vorhanden")
            report.revoked += 1
            continue
        report.valid += 1
    db.flush()
    return report


def revoke_by_twitch_user(db: Session, twitch_user_id: str, reason: str) -> int:
    """EventSub user.authorization.revoke: Nutzer hat die App bei Twitch getrennt."""
    tokens = db.scalars(
        select(OAuthToken).where(
            OAuthToken.provider == OAuthProvider.TWITCH.value,
            OAuthToken.external_user_id == twitch_user_id,
            OAuthToken.revoked_at.is_(None),
        )
    ).all()
    for token in tokens:
        mark_revoked(db, token, reason)
    return len(tokens)


def disconnect(
    db: Session, settings: Settings, http: httpx.Client, token: OAuthToken, *, actor: User
) -> None:
    """Im Dashboard trennen: bei Twitch widerrufen (beste Mühe) und lokal sperren."""
    if token.revoked_at is None:
        revoke_token(settings, http, token.access_token)
    mark_revoked(db, token, f"im Dashboard getrennt von {actor.username}", actor=actor)
