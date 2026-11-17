"""Aktivitäts- und Fehlerprotokoll."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.enums import ActivityCategory, ActivityLevel
from app.models import ActivityLog, Streamer, User
from app.web.deps import require_user
from app.web.templating import render

router = APIRouter()
PAGE_SIZE = 50


@router.get("/activity")
def activity(
    request: Request,
    level: str = "",
    category: str = "",
    streamer_id: int | None = None,
    page: int = 1,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    page = max(1, page)
    stmt = select(ActivityLog).options(selectinload(ActivityLog.user), selectinload(ActivityLog.streamer))
    if level in {lv.value for lv in ActivityLevel}:
        stmt = stmt.where(ActivityLog.level == level)
    if category in {c.value for c in ActivityCategory}:
        stmt = stmt.where(ActivityLog.category == category)
    if streamer_id:
        stmt = stmt.where(ActivityLog.streamer_id == streamer_id)
    stmt = stmt.order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
    entries = db.scalars(stmt.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)).all()
    return render(
        request,
        "activity.html",
        {
            "entries": entries[:PAGE_SIZE],
            "has_next": len(entries) > PAGE_SIZE,
            "page": page,
            "filters": {"level": level, "category": category, "streamer_id": streamer_id},
            "levels": list(ActivityLevel),
            "categories": list(ActivityCategory),
            "streamers": db.scalars(select(Streamer).order_by(Streamer.twitch_login)).all(),
            "nav": "activity",
        },
    )
