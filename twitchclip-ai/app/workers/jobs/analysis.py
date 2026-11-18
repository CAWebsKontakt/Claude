"""RQ-Jobs der KI-Highlight-Erkennung (Phase 4) und Chat-Statistik."""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.ai.transcribe import get_transcriber
from app.config import get_settings
from app.db import session_scope
from app.models import ClipRequest, Highlight
from app.redis_client import get_redis
from app.services.activity import log_activity
from app.services.chat_stats import flush_chat_buckets
from app.services.highlight_analysis import (
    AnalysisError,
    analyze_clip_request,
    group_is_complete,
    select_group,
)
from app.services.streamers import RightsError
from app.workers.queues import QUEUE_MEDIA, make_job_id, safe_enqueue

logger = logging.getLogger(__name__)
JOBS = "app.workers.jobs.analysis"


def schedule_analysis(request_id: int) -> bool:
    return safe_enqueue(
        QUEUE_MEDIA,
        f"{JOBS}.analyze_clip_job",
        request_id,
        job_id=make_job_id("analyze", request_id),
        timeout=3600,
    )


def analyze_clip_job(request_id: int) -> dict:
    settings = get_settings()
    with session_scope() as db:
        request = db.get(ClipRequest, request_id)
        if request is None or request.status != "downloaded" or request.analyzed_at is not None:
            return {"skipped": "Status"}
        transcriber = get_transcriber(settings)
        try:
            highlight = analyze_clip_request(db, settings, request, transcriber)
        except (AnalysisError, RightsError) as exc:
            request.analyzed_at = None
            request.error = f"Analyse: {exc}"[:2000]
            log_activity(
                db,
                "warning",
                "pipeline",
                f"Analyse von Clip-Auftrag #{request.id} übersprungen: {exc}",
                streamer_id=request.streamer_id,
            )
            return {"error": str(exc)}
        group = highlight.selection_group
        selected = None
        if group and group_is_complete(db, group):
            selected = [h.id for h in select_group(db, settings, group)]
        return {"highlight": highlight.id, "score": highlight.score, "selected": selected}


def analyze_pending_job() -> dict:
    """Sicherheitsnetz: heruntergeladene, noch nicht analysierte Clips einreihen."""
    with session_scope() as db:
        ids = list(
            db.scalars(
                select(ClipRequest.id).where(
                    ClipRequest.status == "downloaded",
                    ClipRequest.analyzed_at.is_(None),
                    ClipRequest.file_path.is_not(None),
                    ClipRequest.error.is_(None),
                )
            ).all()
        )
    return {"queued": [i for i in ids if schedule_analysis(i)]}


def reselect_group_job(group: str) -> dict:
    settings = get_settings()
    with session_scope() as db:
        return {"selected": [h.id for h in select_group(db, settings, group)]}


def flush_chat_job() -> dict:
    settings = get_settings()
    if not settings.chat_stats_enabled:
        return {"skipped": True}
    with session_scope() as db:
        return {"buckets": flush_chat_buckets(db, get_redis())}


def highlight_ids_for_group(group: str) -> list[int]:
    with session_scope() as db:
        return list(db.scalars(select(Highlight.id).where(Highlight.selection_group == group)).all())
