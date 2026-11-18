"""TikTok-Warteschlange und veröffentlichte Videos."""

from __future__ import annotations

from datetime import timedelta

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.db import get_db
from app.enums import (
    DONE_STATUSES,
    QUEUE_STATUSES,
    ActivityCategory,
    ActivityLevel,
    HighlightStatus,
    PublishMethod,
    PublishStatus,
)
from app.integrations.tiktok.client import PRIVACY_LABELS, CreatorInfo
from app.integrations.tiktok.errors import TikTokError
from app.media.metadata import TIKTOK_CAPTION_LIMIT
from app.models import Highlight, PublishJob, TikTokAccount, User
from app.services import publishing, tiktok_accounts
from app.services.activity import log_activity
from app.services.streamers import compute_rights
from app.timeutil import utcnow
from app.web.deps import csrf_protect, require_editor, require_user
from app.web.flash import flash
from app.web.templating import render

router = APIRouter()

CANCELLABLE = (PublishStatus.PENDING_REVIEW, PublishStatus.APPROVED, PublishStatus.FAILED)


def _jobs(db: Session, statuses) -> list[PublishJob]:
    return list(
        db.scalars(
            select(PublishJob)
            .where(PublishJob.status.in_([s.value for s in statuses]))
            .options(
                selectinload(PublishJob.highlight).selectinload(Highlight.streamer),
                selectinload(PublishJob.rendered_video),
                selectinload(PublishJob.tiktok_account),
            )
            .order_by(PublishJob.created_at.desc())
            .limit(200)
        ).all()
    )


@router.get("/queue")
def queue(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    accounts = db.scalars(select(TikTokAccount).where(TikTokAccount.is_active.is_(True))).all()
    return render(
        request,
        "queue.html",
        {"jobs": _jobs(db, QUEUE_STATUSES), "accounts": accounts, "cancellable": CANCELLABLE, "nav": "queue"},
    )


@router.get("/published")
def published(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return render(request, "published.html", {"jobs": _jobs(db, DONE_STATUSES), "nav": "published"})


@router.post("/queue/{job_id}/cancel", dependencies=[Depends(csrf_protect)])
def cancel_job(
    request: Request,
    job_id: int,
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    job = db.get(PublishJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Eintrag nicht gefunden")
    if job.status not in CANCELLABLE:
        flash(request, "Dieser Eintrag wird gerade verarbeitet und kann nicht abgebrochen werden.", "error")
        return RedirectResponse("/queue", status_code=303)
    job.status = PublishStatus.CANCELLED.value
    highlight = db.get(Highlight, job.highlight_id)
    if highlight and highlight.status == HighlightStatus.QUEUED:
        highlight.status = HighlightStatus.RENDERED.value
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.TIKTOK,
        f"Warteschlangen-Eintrag #{job.id} abgebrochen",
        user_id=user.id,
        streamer_id=highlight.streamer_id if highlight else None,
    )
    db.commit()
    flash(request, "Eintrag abgebrochen.", "success")
    return RedirectResponse("/queue", status_code=303)


# ---------------------------------------------------------------------------
# Phase 6: in die Warteschlange stellen, prüfen, freigeben, erneut vorbereiten
# ---------------------------------------------------------------------------
CREATOR_INFO_MAX_AGE = timedelta(minutes=15)


@router.post("/highlights/{highlight_id}/queue", dependencies=[Depends(csrf_protect)])
def queue_highlight(
    request: Request,
    highlight_id: int,
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    highlight = db.get(Highlight, highlight_id)
    if highlight is None:
        raise HTTPException(status_code=404, detail="Highlight nicht gefunden")
    existing = publishing.open_job_for(db, highlight)
    if existing is not None:
        return RedirectResponse(f"/queue/{existing.id}", status_code=303)
    try:
        job = publishing.add_to_queue(db, settings, highlight, actor=user)
        db.commit()
    except publishing.PublishError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse(f"/highlights/{highlight_id}", status_code=303)
    flash(request, "In die Warteschlange gestellt. Bitte prüfen und freigeben.", "success")
    return RedirectResponse(f"/queue/{job.id}", status_code=303)


def _job(db: Session, job_id: int) -> PublishJob:
    job = db.scalar(
        select(PublishJob)
        .where(PublishJob.id == job_id)
        .options(
            selectinload(PublishJob.highlight).selectinload(Highlight.streamer),
            selectinload(PublishJob.rendered_video),
            selectinload(PublishJob.tiktok_account),
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Eintrag nicht gefunden")
    return job


def _live_creator_info(db: Session, settings: Settings, account: TikTokAccount | None, *, max_age=None):
    """(CreatorInfo | None, Fehlermeldung | None). Ohne max_age wird immer live abgefragt."""
    if account is None or not account.is_active or not settings.tiktok_configured or settings.test_mode:
        return None, None
    fetched = account.creator_info_fetched_at
    if max_age and account.creator_info and fetched and utcnow() - fetched < max_age:
        return CreatorInfo.from_api(account.creator_info), None
    try:
        with httpx.Client(timeout=20) as http:
            info = tiktok_accounts.refresh_creator_info(db, settings, http, account)
        db.commit()
        return info, None
    except TikTokError as exc:
        db.commit()
        return None, str(exc)


@router.get("/queue/{job_id}")
def review_job(
    request: Request,
    job_id: int,
    account: int | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    job = _job(db, job_id)
    accounts = db.scalars(
        select(TikTokAccount)
        .where(TikTokAccount.is_active.is_(True))
        .options(selectinload(TikTokAccount.oauth_tokens))
        .order_by(TikTokAccount.display_name)
    ).all()
    selected = next((a for a in accounts if a.id == (account or job.tiktok_account_id)), None)
    if selected is None and accounts:
        selected = accounts[0]
    creator_info, creator_error = None, None
    if job.status == PublishStatus.PENDING_REVIEW and user.can_edit:
        # TikTok-Vorgabe: Creator-Info beim Anzeigen der Veröffentlichungsseite live abfragen
        creator_info, creator_error = _live_creator_info(db, settings, selected)
    methods = publishing.available_methods(settings, selected)
    if creator_info is None:
        methods = [m for m in methods if m == PublishMethod.MANUAL_EXPORT]
    video = job.rendered_video
    too_long = bool(
        creator_info
        and video
        and creator_info.max_video_post_duration_sec
        and video.duration > creator_info.max_video_post_duration_sec
    )
    return render(
        request,
        "queue_review.html",
        {
            "job": job,
            "video": video,
            "highlight": job.highlight,
            "rights": compute_rights(job.highlight.streamer),
            "accounts": accounts,
            "selected": selected,
            "creator_info": creator_info,
            "creator_error": creator_error,
            "methods": methods,
            "too_long": too_long,
            "privacy_labels": PRIVACY_LABELS,
            "music_consent": publishing.MUSIC_CONSENT,
            "branded_consent": publishing.BRANDED_CONSENT,
            "label_your_brand": publishing.LABEL_YOUR_BRAND,
            "label_branded": publishing.LABEL_BRANDED,
            "music_url": publishing.MUSIC_URL,
            "branded_url": publishing.BRANDED_URL,
            "caption_limit": TIKTOK_CAPTION_LIMIT,
            "cancellable": CANCELLABLE,
            "nav": "queue",
        },
    )


def _checked(value: str | None) -> bool:
    return value in ("1", "on", "true", "yes")


@router.post("/queue/{job_id}/approve", dependencies=[Depends(csrf_protect)])
def approve_job(
    request: Request,
    job_id: int,
    account_id: str = Form(""),
    method: str = Form(""),
    caption: str = Form(""),
    privacy_level: str = Form(""),
    allow_comment: str | None = Form(None),
    allow_duet: str | None = Form(None),
    allow_stitch: str | None = Form(None),
    commercial: str | None = Form(None),
    your_brand: str | None = Form(None),
    branded_content: str | None = Form(None),
    is_aigc: str | None = Form(None),
    consent: str | None = Form(None),
    rights_confirmed: str | None = Form(None),
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    job = _job(db, job_id)
    account = db.get(TikTokAccount, int(account_id)) if account_id.isdigit() else None
    form = publishing.ApprovalForm(
        account_id=account.id if account else None,
        method=method,
        caption=caption,
        privacy_level=privacy_level or None,
        allow_comment=_checked(allow_comment),
        allow_duet=_checked(allow_duet),
        allow_stitch=_checked(allow_stitch),
        commercial=_checked(commercial),
        your_brand=_checked(your_brand),
        branded_content=_checked(branded_content),
        is_aigc=_checked(is_aigc),
        consent=_checked(consent),
        rights_confirmed=_checked(rights_confirmed),
    )
    back = f"/queue/{job_id}" + (f"?account={account.id}" if account else "")
    creator_info = None
    if method in (PublishMethod.DIRECT_POST, PublishMethod.INBOX_UPLOAD):
        creator_info, error = _live_creator_info(db, settings, account, max_age=CREATOR_INFO_MAX_AGE)
        if creator_info is None:
            flash(request, f"TikTok-Kontoinformationen nicht abrufbar: {error or 'Konto prüfen'}", "error")
            return RedirectResponse(back, status_code=303)
    try:
        publishing.approve(db, settings, job, form, creator_info, actor=user)
        db.commit()
    except publishing.PublishError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse(back, status_code=303)

    if job.method == PublishMethod.MANUAL_EXPORT:
        try:
            publishing.create_export(db, settings, job)
            db.commit()
            flash(request, "Export erstellt – ZIP herunterladen und in der TikTok-App hochladen.", "success")
        except publishing.PublishError as exc:
            db.rollback()
            flash(request, f"Export fehlgeschlagen: {exc}", "error")
        return RedirectResponse(f"/queue/{job_id}", status_code=303)

    from app.workers.jobs.publish import schedule_publish

    if schedule_publish(job.id):
        flash(request, "Freigegeben – das Video wird jetzt an TikTok übertragen.", "success")
    else:
        flash(
            request,
            "Freigegeben. Redis nicht erreichbar – der Upload startet automatisch, sobald es wieder läuft.",
            "warning",
        )
    return RedirectResponse(f"/queue/{job_id}", status_code=303)


@router.post("/queue/{job_id}/retry", dependencies=[Depends(csrf_protect)])
def retry_job(
    request: Request,
    job_id: int,
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    job = _job(db, job_id)
    try:
        publishing.reset_for_retry(db, job, actor=user)
        db.commit()
    except publishing.PublishError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse("/queue", status_code=303)
    flash(request, "Bitte erneut prüfen und freigeben.", "success")
    return RedirectResponse(f"/queue/{job_id}", status_code=303)


@router.post("/queue/{job_id}/refresh-status", dependencies=[Depends(csrf_protect)])
def refresh_status(
    request: Request,
    job_id: int,
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    job = _job(db, job_id)
    if job.publish_id and job.status in (PublishStatus.UPLOADING, PublishStatus.PROCESSING):
        from app.workers.jobs.publish import schedule_status_check

        schedule_status_check(job.id, 1)
        flash(request, "Status wird bei TikTok abgefragt – Seite gleich neu laden.", "success")
    return RedirectResponse(f"/queue/{job_id}", status_code=303)
