"""Aktivitäts- und Fehlerprotokoll (Datenbank + strukturiertes Log)."""

from __future__ import annotations

import logging
from datetime import timedelta

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.enums import ActivityCategory, ActivityLevel
from app.models import ActivityLog
from app.timeutil import utcnow

logger = logging.getLogger("tcai.activity")

_LOG_LEVELS = {
    ActivityLevel.INFO: logging.INFO,
    ActivityLevel.WARNING: logging.WARNING,
    ActivityLevel.ERROR: logging.ERROR,
}
MAX_MESSAGE_LENGTH = 2000


def log_activity(
    db: Session,
    level: ActivityLevel | str,
    category: ActivityCategory | str,
    message: str,
    *,
    details: dict | None = None,
    user_id: int | None = None,
    streamer_id: int | None = None,
    commit: bool = False,
) -> ActivityLog:
    level = ActivityLevel(level)
    category = ActivityCategory(category)
    entry = ActivityLog(
        level=level.value,
        category=category.value,
        message=message[:MAX_MESSAGE_LENGTH],
        details=details or None,
        user_id=user_id,
        streamer_id=streamer_id,
    )
    db.add(entry)
    if commit:
        db.commit()
    else:
        db.flush()
    logger.log(
        _LOG_LEVELS[level],
        message,
        extra={"category": category.value, "user_id": user_id, "streamer_id": streamer_id},
    )
    return entry


def prune_activity(db: Session, retention_days: int) -> int:
    cutoff = utcnow() - timedelta(days=retention_days)
    result = db.execute(delete(ActivityLog).where(ActivityLog.created_at < cutoff))
    return int(result.rowcount or 0)


def log_activity_throttled(
    db: Session,
    redis: Redis,
    key: str,
    ttl_seconds: int,
    level: ActivityLevel | str,
    category: ActivityCategory | str,
    message: str,
    **kwargs,
) -> bool:
    """Wie log_activity, aber höchstens einmal je `ttl_seconds` pro Schlüssel im Dashboard-Protokoll.

    Verhindert, dass ein dauerhaftes Problem (z. B. Twitch nicht erreichbar) bei jedem Polling-Lauf
    einen neuen Eintrag erzeugt. Ins Container-Log wird trotzdem jedes Mal geschrieben.
    """
    try:
        first = bool(redis.set(f"tcai:logthrottle:{key}", "1", nx=True, ex=ttl_seconds))
    except RedisError:
        first = True
    if first:
        log_activity(db, level, category, message, **kwargs)
        return True
    logger.log(
        _LOG_LEVELS[ActivityLevel(level)], message, extra={"category": str(category), "throttled": True}
    )
    return False
