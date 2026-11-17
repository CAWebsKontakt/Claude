"""Übersichtsseite."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.enums import (
    DONE_STATUSES,
    QUEUE_STATUSES,
    ActivityLevel,
    HighlightStatus,
    StreamerMode,
)
from app.models import ActivityLog, Highlight, PublishJob, Streamer, TwitchClip, User
from app.timeutil import local_day_bounds, utcnow
from app.web.deps import require_user
from app.web.templating import render

router = APIRouter()


def _count(db: Session, stmt) -> int:
    return int(db.scalar(stmt) or 0)


@router.get("/")
def dashboard(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    day_start, day_end = local_day_bounds(settings.tz)
    since_24h = utcnow() - timedelta(hours=24)
    stats = {
        "streamers": _count(db, select(func.count(Streamer.id))),
        "streamers_active": _count(db, select(func.count(Streamer.id)).where(Streamer.is_active.is_(True))),
        "streamers_live": _count(db, select(func.count(Streamer.id)).where(Streamer.is_live.is_(True))),
        "streamers_authorized": _count(
            db, select(func.count(Streamer.id)).where(Streamer.mode == StreamerMode.AUTHORIZED.value)
        ),
        "highlights_open": _count(
            db,
            select(func.count(Highlight.id)).where(
                Highlight.status.in_([HighlightStatus.CANDIDATE.value, HighlightStatus.RENDERED.value])
            ),
        ),
        "highlights_today": _count(
            db,
            select(func.count(Highlight.id)).where(
                Highlight.created_at >= day_start,
                Highlight.created_at < day_end,
                Highlight.status.notin_([HighlightStatus.DUPLICATE.value, HighlightStatus.REJECTED.value]),
            ),
        ),
        "clips_recommended": _count(
            db, select(func.count(TwitchClip.id)).where(TwitchClip.status != "dismissed")
        ),
        "queue": _count(
            db,
            select(func.count(PublishJob.id)).where(PublishJob.status.in_([s.value for s in QUEUE_STATUSES])),
        ),
        "published": _count(
            db,
            select(func.count(PublishJob.id)).where(PublishJob.status.in_([s.value for s in DONE_STATUSES])),
        ),
        "errors_24h": _count(
            db,
            select(func.count(ActivityLog.id)).where(
                ActivityLog.level == ActivityLevel.ERROR.value, ActivityLog.created_at >= since_24h
            ),
        ),
    }
    live = db.scalars(
        select(Streamer)
        .where(Streamer.is_live.is_(True))
        .order_by(Streamer.live_viewer_count.desc().nulls_last())
    ).all()
    top_highlights = db.scalars(
        select(Highlight)
        .where(Highlight.status.in_([HighlightStatus.CANDIDATE.value, HighlightStatus.RENDERED.value]))
        .order_by(Highlight.score.desc())
        .limit(5)
    ).all()
    recent = db.scalars(select(ActivityLog).order_by(ActivityLog.created_at.desc()).limit(10)).all()
    return render(
        request,
        "dashboard.html",
        {
            "stats": stats,
            "live": live,
            "top_highlights": top_highlights,
            "recent": recent,
            "max_clips_per_day": settings.max_clips_per_day,
            "nav": "dashboard",
        },
    )
