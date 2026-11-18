"""Streamer hinzufügen/entfernen, Modus, Rechte-Dokumentation."""

from __future__ import annotations

from datetime import date, datetime

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.db import get_db
from app.enums import GrantorRole, StreamerMode
from app.models import (
    ActivityLog,
    ClipRequest,
    EventSubSubscription,
    Highlight,
    OAuthToken,
    Streamer,
    StreamerPermission,
    StreamSession,
    TwitchClip,
    User,
)
from app.services import streamers as streamer_service
from app.services.clip_processing import ClipRequestError, parse_timecode, request_vod_clip, reset_for_retry
from app.services.outreach import build_messages
from app.services.permissions import PermissionError_, PermissionInput, add_permission, revoke_permission
from app.services.streamers import StreamerError, compute_rights
from app.services.twitch_authorization import (
    AuthorizationError,
    create_consent_invite,
    create_invite,
    disconnect,
    publishing_targets,
)
from app.timeutil import utcnow
from app.web.deps import csrf_protect, require_editor, require_user
from app.web.flash import flash
from app.web.templating import render
from app.workers.jobs.clips import schedule_create, schedule_download, schedule_plan
from app.workers.jobs.twitch import schedule_eventsub_sync, schedule_streamer_refresh, twitch_available

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
    if schedule_streamer_refresh(streamer.id):
        hint = "Twitch-Daten, Live-Status und Clips werden jetzt im Hintergrund abgefragt."
    else:
        hint = "Die Twitch-ID wird automatisch abgefragt, sobald die Twitch-API konfiguriert ist."
    flash(request, f"'{streamer.twitch_login}' wurde hinzugefügt (Modus A – öffentlich). {hint}", "success")
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
    subscriptions = db.scalars(
        select(EventSubSubscription)
        .where(EventSubSubscription.streamer_id == streamer.id)
        .order_by(EventSubSubscription.type)
    ).all()
    sessions = db.scalars(
        select(StreamSession)
        .where(StreamSession.streamer_id == streamer.id)
        .order_by(StreamSession.started_at.desc())
        .limit(10)
    ).all()
    clip_requests = db.scalars(
        select(ClipRequest)
        .where(ClipRequest.streamer_id == streamer.id)
        .order_by(ClipRequest.created_at.desc())
        .limit(25)
    ).all()
    twitch_tokens = sorted(
        (t for t in streamer.oauth_tokens if t.provider == "twitch"),
        key=lambda t: (t.revoked_at is None, t.created_at),
        reverse=True,
    )
    invite = request.session.pop("twitch_invite", None)
    invite_url = invite["url"] if invite and invite.get("streamer_id") == streamer.id else None
    consent = request.session.pop("consent_invite", None)
    consent_messages = None
    if consent and consent.get("streamer_id") == streamer.id:
        consent_messages = build_messages(
            channel=streamer.twitch_login,
            display_name=streamer.display_name or streamer.twitch_login,
            link=consent["url"],
            publishing=bool(consent.get("publishing")),
            targets=publishing_targets(db),
        )
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
            "subscriptions": subscriptions,
            "sessions": sessions,
            "clip_requests": clip_requests,
            "twitch_tokens": twitch_tokens,
            "invite_url": invite_url,
            "consent_messages": consent_messages,
            "consent_url": consent["url"] if consent_messages else None,
            "twitch_available": twitch_available(settings),
            "eventsub_active": settings.eventsub_webhooks_active,
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
    schedule_eventsub_sync()
    flash(request, "Überwachung aktualisiert.", "success")
    return _redirect(streamer.id)


@router.post("/{streamer_id}/refresh", dependencies=[Depends(csrf_protect)])
def refresh_from_twitch(
    request: Request,
    streamer_id: int,
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    streamer = _load(db, streamer_id)
    if not twitch_available():
        flash(request, "Twitch-API ist nicht konfiguriert (oder Testmodus aktiv).", "error")
    elif schedule_streamer_refresh(streamer.id):
        flash(request, "Abfrage gestartet – die Seite in einigen Sekunden neu laden.", "success")
    else:
        flash(request, "Eine Abfrage für diesen Kanal läuft bereits.", "info")
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
    schedule_eventsub_sync()  # Abos des entfernten Kanals bei Twitch löschen
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


@router.post("/{streamer_id}/twitch-invite", dependencies=[Depends(csrf_protect)])
def create_twitch_invite(
    request: Request,
    streamer_id: int,
    include_chat: str | None = Form(None),
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    streamer = _load(db, streamer_id)
    try:
        url = create_invite(db, settings, streamer, actor=user, include_chat=include_chat is not None)
        db.commit()
    except AuthorizationError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(streamer.id)
    request.session["twitch_invite"] = {"streamer_id": streamer.id, "url": url}
    flash(request, "Autorisierungslink erstellt – bitte an den Streamer bzw. Editor senden.", "success")
    return _redirect(streamer.id)


@router.post("/{streamer_id}/consent-invite", dependencies=[Depends(csrf_protect)])
def create_consent_link(
    request: Request,
    streamer_id: int,
    offer_publishing: str | None = Form(None),
    include_chat: str | None = Form(None),
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    streamer = _load(db, streamer_id)
    try:
        url = create_consent_invite(
            db,
            settings,
            streamer,
            actor=user,
            offer_publishing=offer_publishing is not None,
            include_chat=include_chat is not None,
        )
        db.commit()
    except AuthorizationError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(streamer.id)
    request.session["consent_invite"] = {
        "streamer_id": streamer.id,
        "url": url,
        "publishing": offer_publishing is not None,
    }
    flash(
        request, "Zustimmungslink erstellt – Nachricht unten kopieren und an den Streamer senden.", "success"
    )
    return _redirect(streamer.id)


@router.post("/{streamer_id}/tokens/{token_id}/disconnect", dependencies=[Depends(csrf_protect)])
def disconnect_token(
    request: Request,
    streamer_id: int,
    token_id: int,
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    streamer = _load(db, streamer_id)
    token = db.get(OAuthToken, token_id)
    if token is None or token.streamer_id != streamer.id:
        raise HTTPException(status_code=404, detail="Autorisierung nicht gefunden")
    with httpx.Client(timeout=10) as http:
        disconnect(db, settings, http, token, actor=user)
    db.commit()
    flash(
        request, "Twitch-Autorisierung getrennt. Die Verarbeitung für diesen Zugang ist gestoppt.", "success"
    )
    return _redirect(streamer.id)


def _load_session(db: Session, streamer: Streamer, session_id: int) -> StreamSession:
    session = db.get(StreamSession, session_id)
    if session is None or session.streamer_id != streamer.id:
        raise HTTPException(status_code=404, detail="Stream nicht gefunden")
    return session


@router.post("/{streamer_id}/sessions/{session_id}/clip", dependencies=[Depends(csrf_protect)])
def request_clip(
    request: Request,
    streamer_id: int,
    session_id: int,
    start: str = Form(""),
    duration: int = Form(30),
    title: str = Form(""),
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    streamer = _load(db, streamer_id)
    session = _load_session(db, streamer, session_id)
    try:
        clip_request = request_vod_clip(
            db,
            streamer,
            session,
            start_seconds=parse_timecode(start),
            duration=duration,
            actor=user,
            title=title.strip() or None,
        )
        db.commit()
    except ClipRequestError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(streamer.id)
    if schedule_create(clip_request.id):
        flash(request, f"Clip-Auftrag #{clip_request.id} angelegt – Twitch erstellt den Clip.", "success")
    else:
        flash(
            request,
            f"Clip-Auftrag #{clip_request.id} angelegt, aber nicht eingereiht (Twitch/Redis).",
            "warning",
        )
    return _redirect(streamer.id)


@router.post("/{streamer_id}/sessions/{session_id}/plan", dependencies=[Depends(csrf_protect)])
def plan_session(
    request: Request,
    streamer_id: int,
    session_id: int,
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    streamer = _load(db, streamer_id)
    session = _load_session(db, streamer, session_id)
    if not compute_rights(streamer).can_process:
        flash(request, "Für diesen Kanal ist keine Verarbeitung erlaubt.", "error")
    elif not session.vod_id:
        flash(request, "Für diesen Stream ist (noch) kein VOD bekannt.", "error")
    elif schedule_plan(session.id, delay_seconds=None, force=True):
        flash(request, "Kandidaten werden aus den Zuschauer-Clips ermittelt.", "success")
    else:
        flash(request, "Die Planung läuft bereits oder Twitch ist nicht konfiguriert.", "info")
    return _redirect(streamer.id)


@router.post("/{streamer_id}/clip-requests/{request_id}/retry", dependencies=[Depends(csrf_protect)])
def retry_clip_request(
    request: Request,
    streamer_id: int,
    request_id: int,
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    streamer = _load(db, streamer_id)
    clip_request = db.get(ClipRequest, request_id)
    if clip_request is None or clip_request.streamer_id != streamer.id:
        raise HTTPException(status_code=404, detail="Auftrag nicht gefunden")
    try:
        step = reset_for_retry(db, clip_request, actor=user)
        db.commit()
    except ClipRequestError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(streamer.id)
    # Neue Job-ID je Neustart, damit alte (fehlgeschlagene) Jobs nicht blockieren
    run = f"r{int(utcnow().timestamp())}"
    if step == "analyze":
        from app.workers.jobs.analysis import schedule_analysis

        queued = schedule_analysis(clip_request.id)
    elif step == "download":
        queued = schedule_download(clip_request.id, run=run)
    else:
        queued = schedule_create(clip_request.id, run=run)
    flash(
        request,
        f"Auftrag #{clip_request.id} wird erneut verarbeitet."
        if queued
        else "Konnte nicht eingereiht werden.",
        "success" if queued else "error",
    )
    return _redirect(streamer.id)
