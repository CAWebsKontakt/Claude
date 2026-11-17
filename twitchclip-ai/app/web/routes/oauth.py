"""Öffentliche OAuth-Seiten für Streamer und Channel-Editoren (kein Dashboard-Login nötig).

Sicherheit: Der Einladungslink ist mit SECRET_KEY signiert und läuft ab; der OAuth-State ist zufällig,
einmalig und 15 Minuten gültig (Schutz gegen CSRF/Replay); jeder Link kann nur einmal erfolgreich
verwendet werden.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.integrations.twitch.errors import TwitchError
from app.integrations.twitch.user_auth import CHAT_SCOPES
from app.models import Streamer
from app.redis_client import get_redis
from app.services.twitch_authorization import (
    AuthorizationError,
    begin_authorization,
    complete_authorization,
    read_invite,
)
from app.web.templating import render

logger = logging.getLogger("tcai.oauth")
router = APIRouter(prefix="/oauth/twitch")

SCOPE_EXPLANATIONS = {
    "channel:manage:clips": "Clips aus Ihren VODs erstellen und Ihre Clips herunterladen (als Kanalinhaber)",
    "editor:manage:clips": "dasselbe als Editor des Kanals",
    "user:read:chat": "Chat mitlesen – gespeichert werden nur Zählwerte, keine Texte oder Namen",
    "user:bot": "als Leser im Chat auftreten (nur lesen, nie schreiben)",
    "channel:bot": "dem Lesezugriff im eigenen Kanal zustimmen",
}


def _error(request: Request, message: str, status_code: int = 400):
    return render(request, "oauth/result.html", {"ok": False, "message": message}, status_code=status_code)


@router.get("/start")
def start(
    request: Request,
    invite: str = "",
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    try:
        data = read_invite(settings, get_redis(), invite)
    except AuthorizationError as exc:
        return _error(request, str(exc))
    streamer = db.get(Streamer, data.streamer_id)
    if streamer is None:
        return _error(request, "Der Kanal ist nicht mehr vorhanden.", 404)
    scopes = ["channel:manage:clips", "editor:manage:clips"] + (
        list(CHAT_SCOPES) if data.include_chat else []
    )
    return render(
        request,
        "oauth/consent.html",
        {
            "streamer": streamer,
            "invite": invite,
            "scopes": [(s, SCOPE_EXPLANATIONS.get(s, s)) for s in scopes],
            "include_chat": data.include_chat,
        },
    )


@router.get("/authorize")
def authorize(request: Request, invite: str = "", settings: Settings = Depends(get_settings)):
    redis = get_redis()
    try:
        data = read_invite(settings, redis, invite)
    except AuthorizationError as exc:
        return _error(request, str(exc))
    return RedirectResponse(begin_authorization(settings, redis, data), status_code=303)


@router.get("/callback")
def callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if error:
        message = (
            "Die Autorisierung wurde abgelehnt."
            if error == "access_denied"
            else f"Twitch meldet: {error_description or error}"
        )
        return _error(request, message)
    if not code or not state:
        return _error(request, "Unvollständige Antwort von Twitch.")
    try:
        with httpx.Client(timeout=15) as http:
            result = complete_authorization(db, settings, get_redis(), http, state=state, code=code)
        db.commit()
    except AuthorizationError as exc:
        db.rollback()
        return _error(request, str(exc))
    except TwitchError as exc:
        db.rollback()
        logger.warning("Twitch-Autorisierung fehlgeschlagen", extra={"error": str(exc)})
        return _error(request, "Twitch hat die Anmeldung nicht bestätigt. Bitte den Link erneut öffnen.", 502)
    return render(request, "oauth/result.html", {"ok": True, "result": result})
