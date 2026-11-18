"""TikTok-Konten (Login Kit mit PKCE) und Creator-Info."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.db import get_db
from app.integrations.tiktok.client import PRIVACY_LABELS
from app.integrations.tiktok.errors import TikTokError
from app.models import TikTokAccount, User
from app.redis_client import get_redis
from app.services import tiktok_accounts
from app.web.deps import csrf_protect, require_editor, require_user
from app.web.flash import flash
from app.web.templating import render

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/tiktok")
def accounts_page(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    accounts = db.scalars(
        select(TikTokAccount)
        .options(selectinload(TikTokAccount.oauth_tokens))
        .order_by(TikTokAccount.is_active.desc(), TikTokAccount.display_name)
    ).all()
    rows = [{"account": a, "token": tiktok_accounts.active_token(a)} for a in accounts]
    return render(
        request,
        "tiktok/accounts.html",
        {
            "rows": rows,
            "privacy_labels": PRIVACY_LABELS,
            "redirect_uri": settings.tiktok_redirect_uri,
            "nav": "tiktok",
        },
    )


@router.post("/oauth/tiktok/start", dependencies=[Depends(csrf_protect)])
def start_connect(
    request: Request,
    user: User = Depends(require_editor),
    settings: Settings = Depends(get_settings),
):
    try:
        url = tiktok_accounts.begin_connect(settings, get_redis(), user)
    except tiktok_accounts.TikTokAccountError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/tiktok", status_code=303)
    return RedirectResponse(url, status_code=303)


@router.get("/oauth/tiktok/callback")
def connect_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if error:
        message = (
            "Die Verbindung wurde in TikTok abgelehnt."
            if error == "access_denied"
            else f"TikTok meldet: {error_description or error}"
        )
        flash(request, message, "error")
        return RedirectResponse("/tiktok", status_code=303)
    if not code or not state:
        flash(request, "Unvollständige Antwort von TikTok.", "error")
        return RedirectResponse("/tiktok", status_code=303)
    try:
        with httpx.Client(timeout=20) as http:
            account = tiktok_accounts.complete_connect(
                db, settings, get_redis(), http, state=state, code=code, user=user
            )
        db.commit()
    except tiktok_accounts.TikTokAccountError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse("/tiktok", status_code=303)
    except TikTokError as exc:
        db.rollback()
        logger.warning("TikTok-Verbindung fehlgeschlagen", extra={"error": str(exc)})
        flash(request, f"TikTok hat die Anmeldung nicht bestätigt: {exc}", "error")
        return RedirectResponse("/tiktok", status_code=303)
    flash(request, f"TikTok-Konto „{account.display_name or account.open_id}“ verbunden.", "success")
    return RedirectResponse("/tiktok", status_code=303)


def _account(db: Session, account_id: int) -> TikTokAccount:
    account = db.get(TikTokAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="TikTok-Konto nicht gefunden")
    return account


@router.post("/tiktok/{account_id}/refresh", dependencies=[Depends(csrf_protect)])
def refresh_account(
    request: Request,
    account_id: int,
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    account = _account(db, account_id)
    try:
        with httpx.Client(timeout=20) as http:
            tiktok_accounts.refresh_creator_info(db, settings, http, account)
        db.commit()
        flash(request, "Kontoinformationen von TikTok aktualisiert.", "success")
    except TikTokError as exc:
        db.commit()  # Markierung „neu verbinden“ behalten
        flash(request, f"TikTok: {exc}", "error")
    return RedirectResponse("/tiktok", status_code=303)


@router.post("/tiktok/{account_id}/disconnect", dependencies=[Depends(csrf_protect)])
def disconnect_account(
    request: Request,
    account_id: int,
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    account = _account(db, account_id)
    with httpx.Client(timeout=15) as http:
        tiktok_accounts.disconnect(db, settings, http, account, actor=user)
    db.commit()
    flash(request, "TikTok-Konto getrennt. Der Zugriff wurde bei TikTok widerrufen.", "success")
    return RedirectResponse("/tiktok", status_code=303)
