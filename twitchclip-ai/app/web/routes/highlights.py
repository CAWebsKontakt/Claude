"""Highlights (KI-Kandidaten) und öffentliche Clip-Empfehlungen (Modus A)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.enums import ActivityCategory, ActivityLevel, ClipStatus, HighlightStatus
from app.models import Highlight, PublishJob, Streamer, TwitchClip, User
from app.services.activity import log_activity
from app.web.deps import csrf_protect, require_editor, require_user
from app.web.flash import flash
from app.web.templating import render

router = APIRouter()

PAGE_SIZE = 30


@router.get("/highlights")
def list_highlights(
    request: Request,
    status: str = "",
    streamer_id: int | None = None,
    min_score: int = 0,
    page: int = 1,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    page = max(1, page)
    stmt = select(Highlight).options(
        selectinload(Highlight.streamer), selectinload(Highlight.rendered_videos)
    )
    if status:
        stmt = stmt.where(Highlight.status == status)
    else:
        stmt = stmt.where(Highlight.status.notin_([HighlightStatus.DUPLICATE.value]))
    if streamer_id:
        stmt = stmt.where(Highlight.streamer_id == streamer_id)
    if min_score:
        stmt = stmt.where(Highlight.score >= min_score)
    stmt = stmt.order_by(Highlight.score.desc(), Highlight.created_at.desc())
    items = db.scalars(stmt.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)).all()
    streamers = db.scalars(select(Streamer).order_by(Streamer.twitch_login)).all()
    return render(
        request,
        "highlights/list.html",
        {
            "items": items[:PAGE_SIZE],
            "has_next": len(items) > PAGE_SIZE,
            "page": page,
            "filters": {"status": status, "streamer_id": streamer_id, "min_score": min_score},
            "statuses": list(HighlightStatus),
            "streamers": streamers,
            "nav": "highlights",
        },
    )


@router.get("/highlights/{highlight_id}")
def highlight_detail(
    request: Request,
    highlight_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    highlight = db.scalar(
        select(Highlight)
        .where(Highlight.id == highlight_id)
        .options(selectinload(Highlight.streamer), selectinload(Highlight.rendered_videos))
    )
    if highlight is None:
        raise HTTPException(status_code=404, detail="Highlight nicht gefunden")
    jobs = db.scalars(
        select(PublishJob)
        .where(PublishJob.highlight_id == highlight.id)
        .order_by(PublishJob.created_at.desc())
    ).all()
    from app.media.metadata import caption_text
    from app.services.publishing import open_job_for
    from app.services.rendering import LAYOUT_CHOICES, RENDERABLE
    from app.services.streamers import compute_rights

    video = highlight.current_video
    rights = compute_rights(highlight.streamer)
    open_job = open_job_for(db, highlight)
    has_srt = bool(video and Path(video.file_path).with_suffix(".srt").is_file())
    return render(
        request,
        "highlights/detail.html",
        {
            "highlight": highlight,
            "video": video,
            "jobs": jobs,
            "caption": caption_text(highlight.description or "", highlight.hashtags or []),
            "layout_choices": LAYOUT_CHOICES,
            "can_render": highlight.status in RENDERABLE and rights.can_process,
            "can_publish": rights.can_publish,
            "open_job": open_job,
            "can_queue": bool(
                video
                and rights.can_publish
                and open_job is None
                and highlight.status == HighlightStatus.RENDERED
            ),
            "has_srt": has_srt,
            "nav": "highlights",
        },
    )


@router.post("/highlights/{highlight_id}/metadata", dependencies=[Depends(csrf_protect)])
def edit_metadata(
    request: Request,
    highlight_id: int,
    title: str = Form(""),
    description: str = Form(""),
    hashtags: str = Form(""),
    rerender: str | None = Form(None),
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    from app.services.rendering import RenderingError, update_metadata

    highlight = db.get(Highlight, highlight_id)
    if highlight is None:
        raise HTTPException(status_code=404, detail="Highlight nicht gefunden")
    try:
        update_metadata(db, highlight, title=title, description=description, hashtags=hashtags, actor=user)
        db.commit()
    except RenderingError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse(f"/highlights/{highlight_id}", status_code=303)
    if rerender is not None:
        return _queue_render(request, highlight, "auto")
    flash(request, "Texte gespeichert. Die Überschrift im Video ändert sich nach „Neu schneiden“.", "success")
    return RedirectResponse(f"/highlights/{highlight_id}", status_code=303)


def _queue_render(request: Request, highlight: Highlight, layout: str):
    from app.timeutil import utcnow
    from app.workers.jobs.render import schedule_render

    if schedule_render(highlight.id, layout, run=f"r{int(utcnow().timestamp())}"):
        flash(request, "Video wird geschnitten – die Seite in etwa einer Minute neu laden.", "success")
    else:
        flash(request, "Konnte nicht eingereiht werden (Redis?).", "error")
    return RedirectResponse(f"/highlights/{highlight.id}", status_code=303)


@router.post("/highlights/{highlight_id}/render", dependencies=[Depends(csrf_protect)])
def rerender_highlight(
    request: Request,
    highlight_id: int,
    layout: str = Form("auto"),
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    from app.services.rendering import LAYOUT_CHOICES, RENDERABLE
    from app.services.streamers import compute_rights

    highlight = db.get(Highlight, highlight_id)
    if highlight is None:
        raise HTTPException(status_code=404, detail="Highlight nicht gefunden")
    if layout not in LAYOUT_CHOICES:
        layout = "auto"
    if highlight.status not in RENDERABLE:
        flash(
            request, "Dieses Highlight kann in seinem aktuellen Status nicht neu geschnitten werden.", "error"
        )
        return RedirectResponse(f"/highlights/{highlight_id}", status_code=303)
    if not compute_rights(highlight.streamer).can_process:
        flash(request, "Für diesen Kanal ist keine Verarbeitung erlaubt.", "error")
        return RedirectResponse(f"/highlights/{highlight_id}", status_code=303)
    return _queue_render(request, highlight, layout)


@router.post("/highlights/{highlight_id}/reject", dependencies=[Depends(csrf_protect)])
def reject_highlight(
    request: Request,
    highlight_id: int,
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    highlight = db.get(Highlight, highlight_id)
    if highlight is None:
        raise HTTPException(status_code=404, detail="Highlight nicht gefunden")
    if highlight.status in (HighlightStatus.QUEUED, HighlightStatus.PUBLISHED):
        flash(
            request,
            "Highlights in der Warteschlange oder bereits veröffentlichte können nicht verworfen werden.",
            "error",
        )
    else:
        highlight.status = HighlightStatus.REJECTED.value
        log_activity(
            db,
            ActivityLevel.INFO,
            ActivityCategory.PIPELINE,
            f"Highlight #{highlight.id} verworfen",
            user_id=user.id,
            streamer_id=highlight.streamer_id,
        )
        db.commit()
        flash(request, "Highlight verworfen.", "success")
    return RedirectResponse(f"/highlights/{highlight_id}", status_code=303)


@router.post("/highlights/{highlight_id}/restore", dependencies=[Depends(csrf_protect)])
def restore_highlight(
    request: Request,
    highlight_id: int,
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    highlight = db.get(Highlight, highlight_id)
    if highlight is None:
        raise HTTPException(status_code=404, detail="Highlight nicht gefunden")
    if highlight.status == HighlightStatus.REJECTED:
        highlight.status = (
            HighlightStatus.RENDERED.value if highlight.current_video else HighlightStatus.CANDIDATE.value
        )
        db.commit()
        flash(request, "Highlight wiederhergestellt.", "success")
    return RedirectResponse(f"/highlights/{highlight_id}", status_code=303)


@router.get("/clips")
def list_clips(
    request: Request,
    streamer_id: int | None = None,
    show_dismissed: int = 0,
    page: int = 1,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    page = max(1, page)
    stmt = select(TwitchClip).options(selectinload(TwitchClip.streamer))
    if streamer_id:
        stmt = stmt.where(TwitchClip.streamer_id == streamer_id)
    if not show_dismissed:
        stmt = stmt.where(TwitchClip.status != ClipStatus.DISMISSED.value)
    stmt = stmt.order_by(TwitchClip.discovery_score.desc(), TwitchClip.clip_created_at.desc())
    items = db.scalars(stmt.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)).all()
    streamers = db.scalars(select(Streamer).order_by(Streamer.twitch_login)).all()
    return render(
        request,
        "clips.html",
        {
            "items": items[:PAGE_SIZE],
            "has_next": len(items) > PAGE_SIZE,
            "page": page,
            "filters": {"streamer_id": streamer_id, "show_dismissed": show_dismissed},
            "streamers": streamers,
            "embed_parent": request.url.hostname or "localhost",
            "nav": "clips",
        },
    )


@router.post("/clips/{clip_id}/dismiss", dependencies=[Depends(csrf_protect)])
def dismiss_clip(
    request: Request,
    clip_id: int,
    user: User = Depends(require_editor),
    db: Session = Depends(get_db),
):
    clip = db.get(TwitchClip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip nicht gefunden")
    clip.status = ClipStatus.DISMISSED.value
    db.commit()
    flash(request, "Clip ausgeblendet.", "success")
    return RedirectResponse("/clips", status_code=303)
