"""Hype-Liste im Dashboard: wachsende Kanäle finden und gezielt um Zustimmung bitten."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import User
from app.redis_client import get_redis
from app.services import streamers as streamer_service
from app.services.hype import find_entry, latest
from app.services.twitch_authorization import AuthorizationError, create_consent_invite
from app.web.deps import csrf_protect, require_editor, require_user
from app.web.flash import flash
from app.web.templating import render
from app.workers.queues import QUEUE_DEFAULT, enqueue_unique

router = APIRouter(prefix="/hype")


@router.get("")
def hype_page(
    request: Request,
    game: str = "",
    nur_neue: int = 0,
    user: User = Depends(require_user),
    settings: Settings = Depends(get_settings),
):
    result = latest(get_redis())
    entries = result.entries if result else []
    games = sorted({e.game for e in entries if e.game})
    if game:
        entries = [e for e in entries if e.game == game]
    if nur_neue:
        entries = [e for e in entries if e.streamer_id is None]
    return render(
        request,
        "hype.html",
        {
            "result": result,
            "entries": entries,
            "games": games,
            "filters": {"game": game, "nur_neue": nur_neue},
            "nav": "hype",
        },
    )


@router.post("/refresh", dependencies=[Depends(csrf_protect)])
def refresh(
    request: Request, user: User = Depends(require_editor), settings: Settings = Depends(get_settings)
):
    if not settings.twitch_configured or settings.test_mode:
        flash(request, "Dafür muss die Twitch-API konfiguriert und der Testmodus aus sein.", "error")
        return RedirectResponse("/hype", status_code=303)
    try:
        _job, created = enqueue_unique(
            QUEUE_DEFAULT, "app.workers.jobs.twitch.hype_scan_job", job_id="manual-hype_scan"
        )
        flash(
            request,
            "Hype-Liste wird aktualisiert – Seite in etwa 30 Sekunden neu laden."
            if created
            else "Eine Aktualisierung läuft bereits.",
            "success" if created else "info",
        )
    except RedisError:
        flash(request, "Redis nicht erreichbar.", "error")
    return RedirectResponse("/hype", status_code=303)


def _ensure_streamer(db: Session, entry, user: User):
    streamer = streamer_service.get_by_login(db, entry.login)
    if streamer is None:
        streamer = streamer_service.add_streamer(
            db, entry.login, actor=user, notes="über die Hype-Liste gefunden"
        )
    streamer.twitch_user_id = streamer.twitch_user_id or entry.user_id
    streamer.display_name = streamer.display_name or entry.name
    return streamer


@router.post("/{user_id}/watch", dependencies=[Depends(csrf_protect)])
def watch(
    request: Request, user_id: str, user: User = Depends(require_editor), db: Session = Depends(get_db)
):
    entry = find_entry(get_redis(), user_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Kanal nicht (mehr) in der Hype-Liste")
    streamer = _ensure_streamer(db, entry, user)
    db.commit()
    flash(request, f"„{entry.name}“ wird beobachtet (Modus A – nur öffentliche Daten).", "success")
    return RedirectResponse(f"/streamers/{streamer.id}", status_code=303)


@router.post("/{user_id}/request", dependencies=[Depends(csrf_protect)])
def request_consent(
    request: Request,
    user_id: str,
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Kanal aufnehmen (Modus A), Zustimmungslink erzeugen und die fertigen Nachrichten anzeigen."""
    entry = find_entry(get_redis(), user_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Kanal nicht (mehr) in der Hype-Liste")
    streamer = _ensure_streamer(db, entry, user)
    try:
        url = create_consent_invite(
            db, settings, streamer, actor=user, offer_publishing=True, include_chat=False
        )
        db.commit()
    except AuthorizationError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse("/hype", status_code=303)
    request.session["consent_invite"] = {"streamer_id": streamer.id, "url": url, "publishing": True}
    flash(
        request, "Zustimmungslink erstellt – Nachricht kopieren und selbst an den Streamer senden.", "success"
    )
    return RedirectResponse(f"/streamers/{streamer.id}#zustimmung", status_code=303)
