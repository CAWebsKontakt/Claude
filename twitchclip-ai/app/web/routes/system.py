"""Systemstatus und Betriebsaktionen."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.integrations.twitch.ratelimit import RateLimiter
from app.models import EventSubSubscription, User
from app.redis_client import get_redis
from app.services.alerts import active_alerts, last_run
from app.services.system import collect_status
from app.services.twitch_monitor import read_status
from app.web.deps import csrf_protect, require_admin, require_user
from app.web.flash import flash
from app.web.templating import render
from app.workers.queues import QUEUE_DEFAULT, enqueue_unique

router = APIRouter()


def _last_at(redis):
    from datetime import datetime

    info = last_run(redis)
    try:
        return datetime.fromisoformat(info["at"]) if info else None
    except (KeyError, ValueError):
        return None


@router.get("/system")
def system(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    redis = get_redis()
    status = collect_status(settings, db, redis, detailed=True)
    eventsub_count = db.scalar(
        select(func.count(EventSubSubscription.id)).where(EventSubSubscription.status == "enabled")
    )
    return render(
        request,
        "system.html",
        {
            "status": status,
            "config": settings.public_summary(),
            "twitch": read_status(redis),
            "ratelimit": RateLimiter(redis, "app").state(),
            "eventsub_count": eventsub_count or 0,
            "alerts": active_alerts(redis),
            "alerts_last_at": _last_at(redis),
            "nav": "system",
        },
    )


@router.post("/system/cleanup", dependencies=[Depends(csrf_protect)])
def trigger_cleanup(request: Request, admin: User = Depends(require_admin)):
    try:
        _job, created = enqueue_unique(
            QUEUE_DEFAULT, "app.workers.jobs.maintenance.cleanup_storage_job", job_id="manual-storage_cleanup"
        )
        flash(
            request,
            "Speicherbereinigung wurde gestartet." if created else "Eine Speicherbereinigung läuft bereits.",
            "success" if created else "info",
        )
    except RedisError:
        flash(request, "Redis nicht erreichbar – Job konnte nicht gestartet werden.", "error")
    return RedirectResponse("/system", status_code=303)


@router.post("/system/alerts", dependencies=[Depends(csrf_protect)])
def trigger_alerts(request: Request, admin: User = Depends(require_admin)):
    try:
        _job, created = enqueue_unique(
            QUEUE_DEFAULT, "app.workers.jobs.maintenance.alerts_job", job_id="manual-alerts"
        )
        flash(request, "Prüfung gestartet – Seite in einigen Sekunden neu laden.", "success")
    except RedisError:
        flash(request, "Redis nicht erreichbar – Job konnte nicht gestartet werden.", "error")
    return RedirectResponse("/system", status_code=303)
