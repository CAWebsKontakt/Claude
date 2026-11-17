"""TikTok-Warteschlange und veröffentlichte Videos."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.enums import (
    DONE_STATUSES,
    QUEUE_STATUSES,
    ActivityCategory,
    ActivityLevel,
    HighlightStatus,
    PublishStatus,
)
from app.models import Highlight, PublishJob, TikTokAccount, User
from app.services.activity import log_activity
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
