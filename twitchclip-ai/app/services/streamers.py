"""Streamer-Verwaltung und Berechnung des Rechte-/Autorisierungsstatus."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import (
    ActivityCategory,
    ActivityLevel,
    OAuthProvider,
    RightsStatus,
    StreamerMode,
)
from app.models import OAuthToken, Streamer, StreamerPermission, User
from app.services.activity import log_activity
from app.timeutil import utcnow

# Twitch-Logins: 3–25 Zeichen (ältere Konten haben teils 3), Buchstaben, Ziffern, Unterstrich.
LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9_]{2,24}$")

# Scopes, die für VOD-Clips und Clip-Downloads (Modus B) nötig sind – einer davon genügt.
TWITCH_CLIP_SCOPES = frozenset({"editor:manage:clips", "channel:manage:clips"})


class StreamerError(ValueError):
    pass


def normalize_login(raw: str) -> str:
    """Akzeptiert 'Name', '@name' oder 'https://www.twitch.tv/name'."""
    value = (raw or "").strip()
    if "twitch.tv" in value:
        if not value.startswith(("http://", "https://")):
            value = "https://" + value
        path = urlparse(value).path.strip("/")
        value = path.split("/")[0] if path else ""
    value = value.lstrip("@").strip().lower()
    if not LOGIN_RE.match(value):
        raise StreamerError(f"'{raw}' ist kein gültiger Twitch-Benutzername.")
    return value


def get_by_login(db: Session, login: str) -> Streamer | None:
    return db.scalar(select(Streamer).where(Streamer.twitch_login == login))


def add_streamer(
    db: Session,
    raw_login: str,
    *,
    actor: User | None = None,
    notes: str | None = None,
) -> Streamer:
    login = normalize_login(raw_login)
    if get_by_login(db, login):
        raise StreamerError(f"Streamer '{login}' ist bereits vorhanden.")
    # Neue Streamer starten immer in Modus A. Modus B erfordert dokumentierte Rechte.
    streamer = Streamer(
        twitch_login=login,
        mode=StreamerMode.PUBLIC.value,
        notes=(notes or "").strip() or None,
        added_by_user_id=actor.id if actor else None,
    )
    db.add(streamer)
    db.flush()
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.STREAMER,
        f"Streamer '{login}' hinzugefügt (Modus A – öffentlich)",
        user_id=actor.id if actor else None,
        streamer_id=streamer.id,
    )
    return streamer


def remove_streamer(db: Session, streamer: Streamer, *, actor: User) -> None:
    login = streamer.twitch_login
    log_activity(
        db,
        ActivityLevel.WARNING,
        ActivityCategory.STREAMER,
        f"Streamer '{login}' entfernt (inkl. Rechte-Nachweisen, Tokens und Highlights)",
        user_id=actor.id,
        details={"twitch_login": login, "twitch_user_id": streamer.twitch_user_id},
    )
    db.delete(streamer)
    db.flush()


def seed_from_list(db: Session, logins: list[str]) -> list[str]:
    """Legt Streamer aus STREAMER_LIST an, die noch nicht existieren. Entfernt nie etwas."""
    added: list[str] = []
    for raw in logins:
        try:
            login = normalize_login(raw)
        except StreamerError:
            log_activity(
                db,
                ActivityLevel.WARNING,
                ActivityCategory.STREAMER,
                f"STREAMER_LIST: '{raw}' ist ungültig und wurde übersprungen",
            )
            continue
        if get_by_login(db, login) is None:
            add_streamer(db, login, notes="Aus STREAMER_LIST importiert")
            added.append(login)
    return added


def set_active(db: Session, streamer: Streamer, active: bool, *, actor: User) -> None:
    streamer.is_active = active
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.STREAMER,
        f"Überwachung von '{streamer.twitch_login}' {'aktiviert' if active else 'pausiert'}",
        user_id=actor.id,
        streamer_id=streamer.id,
    )


def active_permission(streamer: Streamer, moment: datetime | None = None) -> StreamerPermission | None:
    moment = moment or utcnow()
    valid = [p for p in streamer.permissions if p.allows_processing and p.is_valid_at(moment)]
    if not valid:
        return None
    # Bevorzugt eine Erlaubnis, die auch Veröffentlichung abdeckt.
    valid.sort(key=lambda p: (p.allows_publishing, p.valid_from), reverse=True)
    return valid[0]


def active_twitch_token(streamer: Streamer) -> OAuthToken | None:
    """Gültiges Twitch-Token mit Clip-Berechtigung; das des Kanalinhabers wird bevorzugt."""
    candidates = [
        token
        for token in streamer.oauth_tokens
        if token.provider == OAuthProvider.TWITCH.value
        and token.revoked_at is None
        and token.has_any_scope(TWITCH_CLIP_SCOPES)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda t: (t.role == "broadcaster", t.created_at), reverse=True)
    return candidates[0]


@dataclass
class RightsInfo:
    status: RightsStatus
    label: str
    tone: str  # für die Badge-Farbe: neutral | warn | ok | good
    can_process: bool = False
    can_publish: bool = False
    reasons: list[str] = field(default_factory=list)
    permission: StreamerPermission | None = None
    token: OAuthToken | None = None


def compute_rights(streamer: Streamer, moment: datetime | None = None) -> RightsInfo:
    """Einzige Quelle der Wahrheit dafür, was mit den Inhalten eines Streamers erlaubt ist.

    Jeder Verarbeitungsschritt (Download, Analyse, Schnitt, Upload) prüft diesen Status
    unmittelbar vor der Ausführung erneut.
    """
    moment = moment or utcnow()
    if streamer.is_demo:
        if get_settings().test_mode:
            return RightsInfo(
                status=RightsStatus.READY_PUBLISHING,
                label="Testmodus – eigene Beispieldateien",
                tone="good",
                can_process=True,
                can_publish=True,
                reasons=["Demo-Kanal: verarbeitet nur Dateien aus /data/samples, keine Twitch-Inhalte."],
            )
        return RightsInfo(
            status=RightsStatus.PUBLIC_ONLY,
            label="Demo-Kanal (Testmodus aus)",
            tone="neutral",
            reasons=["Der Demo-Kanal wird nur bei TEST_MODE=true verarbeitet."],
        )
    if streamer.mode != StreamerMode.AUTHORIZED:
        return RightsInfo(
            status=RightsStatus.PUBLIC_ONLY,
            label="Nur öffentliche Metadaten",
            tone="neutral",
            reasons=[
                "Modus A: Es werden nur Links, Vorschaubilder und Empfehlungen angezeigt.",
                "Kein Download, keine Bearbeitung, keine Veröffentlichung.",
            ],
        )

    permission = active_permission(streamer, moment)
    token = active_twitch_token(streamer)
    if permission is None:
        return RightsInfo(
            status=RightsStatus.MISSING_PERMISSION,
            label="Erlaubnis fehlt",
            tone="warn",
            reasons=["Keine gültige, dokumentierte Erlaubnis zur Weiterverarbeitung vorhanden."],
            token=token,
        )
    if token is None:
        return RightsInfo(
            status=RightsStatus.MISSING_OAUTH,
            label="Twitch-Autorisierung fehlt",
            tone="warn",
            reasons=[
                "Erlaubnis ist dokumentiert, aber Streamer/Editor hat noch keinen OAuth-Zugriff "
                "mit 'editor:manage:clips' oder 'channel:manage:clips' gewährt."
            ],
            permission=permission,
        )
    if permission.allows_publishing:
        return RightsInfo(
            status=RightsStatus.READY_PUBLISHING,
            label="Verarbeitung & Veröffentlichung erlaubt",
            tone="good",
            can_process=True,
            can_publish=True,
            permission=permission,
            token=token,
        )
    return RightsInfo(
        status=RightsStatus.READY_PROCESSING,
        label="Nur Verarbeitung erlaubt",
        tone="ok",
        can_process=True,
        can_publish=False,
        reasons=["Die dokumentierte Erlaubnis umfasst keine Veröffentlichung."],
        permission=permission,
        token=token,
    )


class RightsError(PermissionError):
    """Verarbeitung ist für diesen Streamer (derzeit) nicht erlaubt."""


def ensure_can_process(streamer: Streamer) -> RightsInfo:
    """Harte Prüfung unmittelbar vor jedem Download-/Analyse-/Schnitt-Schritt."""
    rights = compute_rights(streamer)
    if not rights.can_process:
        raise RightsError(f"{rights.label}: " + " ".join(rights.reasons))
    return rights


def ensure_can_publish(streamer: Streamer) -> RightsInfo:
    rights = compute_rights(streamer)
    if not rights.can_publish:
        raise RightsError(f"{rights.label}: Veröffentlichung nicht erlaubt. " + " ".join(rights.reasons))
    return rights


def set_mode(db: Session, streamer: Streamer, mode: StreamerMode | str, *, actor: User) -> None:
    try:
        mode = StreamerMode(mode)
    except ValueError as exc:
        raise StreamerError("Unbekannter Modus") from exc
    if mode == streamer.mode:
        return
    if mode == StreamerMode.AUTHORIZED and active_permission(streamer) is None:
        raise StreamerError(
            "Modus B erfordert zuerst eine dokumentierte, gültige Erlaubnis des Streamers "
            "oder eines berechtigten Editors."
        )
    streamer.mode = mode.value
    label = "Modus B (autorisiert)" if mode == StreamerMode.AUTHORIZED else "Modus A (öffentlich)"
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.STREAMER,
        f"'{streamer.twitch_login}' auf {label} umgestellt",
        user_id=actor.id,
        streamer_id=streamer.id,
    )
