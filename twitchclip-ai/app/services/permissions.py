"""Dokumentation der Erlaubnis zur Weiterverarbeitung und Veröffentlichung (Voraussetzung für Modus B)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from sqlalchemy.orm import Session

from app.enums import ActivityCategory, ActivityLevel, GrantorRole, StreamerMode
from app.models import Streamer, StreamerPermission, User
from app.services.activity import log_activity
from app.services.streamers import active_permission
from app.timeutil import utcnow


class PermissionError_(ValueError):
    """Ungültige Rechte-Dokumentation (Name vermeidet Kollision mit builtin PermissionError)."""


@dataclass
class PermissionInput:
    granted_by_name: str
    granted_by_role: str
    evidence_reference: str
    allows_processing: bool
    allows_publishing: bool
    confirmed: bool
    contact: str | None = None
    scope_note: str | None = None
    valid_from: date | None = None
    valid_until: date | None = None


def _start_of_day(value: date, tz) -> datetime:
    return datetime.combine(value, time.min, tzinfo=tz).astimezone(UTC)


def _end_of_day(value: date, tz) -> datetime:
    return datetime.combine(value, time.max, tzinfo=tz).astimezone(UTC)


def add_permission(
    db: Session, streamer: Streamer, data: PermissionInput, *, actor: User, tz
) -> StreamerPermission:
    name = (data.granted_by_name or "").strip()
    evidence = (data.evidence_reference or "").strip()
    if len(name) < 2:
        raise PermissionError_("Bitte angeben, wer die Erlaubnis erteilt hat.")
    if len(evidence) < 5:
        raise PermissionError_(
            "Bitte den Nachweis beschreiben (z. B. Vertrag, E-Mail vom …, Ablageort des Dokuments)."
        )
    try:
        role = GrantorRole(data.granted_by_role)
    except ValueError as exc:
        raise PermissionError_("Unbekannte Rolle des Erlaubnisgebers") from exc
    if data.allows_publishing and not data.allows_processing:
        raise PermissionError_("Veröffentlichung setzt die Erlaubnis zur Verarbeitung voraus.")
    if not data.allows_processing:
        raise PermissionError_("Mindestens die Verarbeitung muss erlaubt sein.")
    if not data.confirmed:
        raise PermissionError_(
            "Bitte bestätigen, dass die Erlaubnis schriftlich vorliegt und nachgewiesen werden kann."
        )
    valid_from = _start_of_day(data.valid_from, tz) if data.valid_from else utcnow()
    valid_until = _end_of_day(data.valid_until, tz) if data.valid_until else None
    if valid_until is not None and valid_until <= valid_from:
        raise PermissionError_("'Gültig bis' muss nach 'Gültig ab' liegen.")

    permission = StreamerPermission(
        streamer_id=streamer.id,
        granted_by_name=name[:200],
        granted_by_role=role.value,
        contact=(data.contact or "").strip()[:300] or None,
        allows_processing=True,
        allows_publishing=bool(data.allows_publishing),
        evidence_reference=evidence,
        scope_note=(data.scope_note or "").strip() or None,
        valid_from=valid_from,
        valid_until=valid_until,
        recorded_by_user_id=actor.id,
    )
    db.add(permission)
    db.flush()
    db.refresh(streamer)
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.PERMISSION,
        f"Erlaubnis für '{streamer.twitch_login}' dokumentiert "
        f"({'Verarbeitung + Veröffentlichung' if permission.allows_publishing else 'nur Verarbeitung'}, "
        f"erteilt von {permission.granted_by_name})",
        user_id=actor.id,
        streamer_id=streamer.id,
        details={"permission_id": permission.id, "evidence": evidence[:500]},
    )
    return permission


def revoke_permission(
    db: Session, streamer: Streamer, permission: StreamerPermission, reason: str, *, actor: User
) -> bool:
    """Widerruft eine Erlaubnis. Gibt True zurück, wenn der Streamer dadurch auf Modus A zurückfällt."""
    if permission.streamer_id != streamer.id:
        raise PermissionError_("Erlaubnis gehört nicht zu diesem Streamer.")
    if permission.revoked_at is not None:
        raise PermissionError_("Diese Erlaubnis wurde bereits widerrufen.")
    permission.revoked_at = utcnow()
    permission.revoked_reason = (reason or "").strip() or "ohne Angabe"
    db.flush()
    db.refresh(streamer)
    downgraded = False
    if streamer.mode == StreamerMode.AUTHORIZED and active_permission(streamer) is None:
        streamer.mode = StreamerMode.PUBLIC.value
        downgraded = True
    log_activity(
        db,
        ActivityLevel.WARNING,
        ActivityCategory.PERMISSION,
        f"Erlaubnis #{permission.id} für '{streamer.twitch_login}' widerrufen: {permission.revoked_reason}"
        + (" – Streamer automatisch auf Modus A zurückgesetzt" if downgraded else ""),
        user_id=actor.id,
        streamer_id=streamer.id,
    )
    return downgraded
