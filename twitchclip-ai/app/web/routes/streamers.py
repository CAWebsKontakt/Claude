"""Streamer hinzufügen/entfernen, Modus, Rechte-Dokumentation."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.db import get_db
from app.enums import GrantorRole, StreamerMode
from app.models import ActivityLog, Highlight, Streamer, StreamerPermission, TwitchClip, User
from app.services import streamers as streamer_service
from app.services.permissions import PermissionError_, PermissionInput, add_permission, revoke_permission
from app.services.streamers import StreamerError, compute_rights
from app.web.deps import csrf_protect, require_editor, require_user
from app.web.flash import flash
from app.web.templating import render

router = APIRouter(prefix="/streamers")


def _load(db: Session, streamer_id: int) -> Streamer:
    streamer = db.scalar(
        select(Streamer)
        .where(Streamer.id == streamer_id)
        .options(selectinload(Streamer.permissions), selectinload(Streamer.oauth_tokens))
    )
    if streamer is None:
        raise HTTPException(status_code=404, detail="Streamer nicht gefunden")
    return streamer


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PermissionError_(f"Ungültiges Datum: {value}") from exc


def _redirect(streamer_id: int | None = None) -> RedirectResponse:
    return RedirectResponse(f"/streamers/{streamer_id}" if streamer_id else "/streamers", status_code=303)


@router.get("")
def list_streamers(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    streamers = db.scalars(
        select(Streamer)
        .options(selectinload(Streamer.permissions), selectinload(Streamer.oauth_tokens))
        .order_by(Streamer.is_live.desc(), Streamer.twitch_login)
    ).all()
    highlight_counts = dict(
        db.execute(
            select(Highlight.streamer_id, func.count(Highlight.id)).group_by(Highlight.streamer_id)
        ).all()
    )
    rows = [(s, compute_rights(s), highlight_counts.get(s.id, 0)) for s in streamers]
    return render(request, "streamers/list.html", {"rows": rows, "nav": "streamers"})


@router.post("", dependencies=[Depends(csrf_protect)])
def create_streamer(
    request: Request,
    login: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    try:
        streamer = streamer_service.add_streamer(db, login, actor=user, notes=notes)
        db.commit()
    except StreamerError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect()
    flash(
        request,
        f"'{streamer.twitch_login}' wurde hinzugefügt (Modus A – öffentlich). "
        "Die Twitch-ID wird automatisch abgefragt, sobald die Twitch-API konfiguriert ist.",
        "success",
    )
    return _redirect(streamer.id)


@router.get("/{streamer_id}")
def streamer_detail(
    request: Request,
    streamer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    streamer = _load(db, streamer_id)
    highlights = db.scalars(
        select(Highlight)
        .where(Highlight.streamer_id == streamer.id)
        .order_by(Highlight.created_at.desc())
        .limit(10)
    ).all()
    clips = db.scalars(
        select(TwitchClip)
        .where(TwitchClip.streamer_id == streamer.id)
        .order_by(TwitchClip.discovery_score.desc())
        .limit(6)
    ).all()
    activity = db.scalars(
        select(ActivityLog)
        .where(ActivityLog.streamer_id == streamer.id)
        .order_by(ActivityLog.created_at.desc())
        .limit(15)
    ).all()
    return render(
        request,
        "streamers/detail.html",
        {
            "streamer": streamer,
            "rights": compute_rights(streamer),
            "highlights": highlights,
            "clips": clips,
            "activity": activity,
            "grantor_roles": list(GrantorRole),
            "today": datetime.now(settings.tz).date().isoformat(),
            "nav": "streamers",
        },
    )


@router.post("/{streamer_id}/active", dependencies=[Depends(csrf_protect)])
def toggle_active(
    request: Request,
    streamer_id: int,
    active: str = Form("0"),
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    streamer = _load(db, streamer_id)
    streamer_service.set_active(db, streamer, active == "1", actor=user)
    db.commit()
    flash(request, "Überwachung aktualisiert.", "success")
    return _redirect(streamer.id)


@router.post("/{streamer_id}/mode", dependencies=[Depends(csrf_protect)])
def change_mode(
    request: Request,
    streamer_id: int,
    mode: str = Form(...),
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    streamer = _load(db, streamer_id)
    try:
        streamer_service.set_mode(db, streamer, mode, actor=user)
        db.commit()
        flash(request, "Betriebsart geändert.", "success")
    except StreamerError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return _redirect(streamer.id)


@router.post("/{streamer_id}/notes", dependencies=[Depends(csrf_protect)])
def update_notes(
    request: Request,
    streamer_id: int,
    notes: str = Form(""),
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    streamer = _load(db, streamer_id)
    streamer.notes = notes.strip()[:5000] or None
    db.commit()
    flash(request, "Notizen gespeichert.", "success")
    return _redirect(streamer.id)


@router.post("/{streamer_id}/delete", dependencies=[Depends(csrf_protect)])
def delete_streamer(
    request: Request,
    streamer_id: int,
    confirm_login: str = Form(""),
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    streamer = _load(db, streamer_id)
    if confirm_login.strip().lower() != streamer.twitch_login:
        flash(request, "Zum Entfernen bitte den Twitch-Namen exakt eingeben.", "error")
        return _redirect(streamer.id)
    login = streamer.twitch_login
    streamer_service.remove_streamer(db, streamer, actor=user)
    db.commit()
    flash(request, f"'{login}' wurde entfernt.", "success")
    return _redirect()


@router.post("/{streamer_id}/permissions", dependencies=[Depends(csrf_protect)])
def create_permission(
    request: Request,
    streamer_id: int,
    granted_by_name: str = Form(""),
    granted_by_role: str = Form(GrantorRole.BROADCASTER.value),
    contact: str = Form(""),
    evidence_reference: str = Form(""),
    scope_note: str = Form(""),
    allows_processing: str | None = Form(None),
    allows_publishing: str | None = Form(None),
    confirmed: str | None = Form(None),
    valid_from: str = Form(""),
    valid_until: str = Form(""),
    switch_to_authorized: str | None = Form(None),
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    streamer = _load(db, streamer_id)
    try:
        data = PermissionInput(
            granted_by_name=granted_by_name,
            granted_by_role=granted_by_role,
            contact=contact,
            evidence_reference=evidence_reference,
            scope_note=scope_note,
            allows_processing=allows_processing is not None,
            allows_publishing=allows_publishing is not None,
            confirmed=confirmed is not None,
            valid_from=_parse_date(valid_from),
            valid_until=_parse_date(valid_until),
        )
        add_permission(db, streamer, data, actor=user, tz=settings.tz)
        if switch_to_authorized is not None:
            streamer_service.set_mode(db, streamer, StreamerMode.AUTHORIZED, actor=user)
        db.commit()
        flash(request, "Erlaubnis dokumentiert.", "success")
    except (PermissionError_, StreamerError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return _redirect(streamer_id)


@router.post("/{streamer_id}/permissions/{permission_id}/revoke", dependencies=[Depends(csrf_protect)])
def revoke(
    request: Request,
    streamer_id: int,
    permission_id: int,
    reason: str = Form(""),
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    streamer = _load(db, streamer_id)
    permission = db.get(StreamerPermission, permission_id)
    if permission is None or permission.streamer_id != streamer.id:
        raise HTTPException(status_code=404, detail="Erlaubnis nicht gefunden")
    try:
        downgraded = revoke_permission(db, streamer, permission, reason, actor=user)
        db.commit()
        flash(
            request,
            "Erlaubnis widerrufen." + (" Der Streamer läuft jetzt wieder in Modus A." if downgraded else ""),
            "warning" if downgraded else "success",
        )
    except PermissionError_ as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return _redirect(streamer.id)
