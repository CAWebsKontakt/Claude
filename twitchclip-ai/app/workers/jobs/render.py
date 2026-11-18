"""RQ-Jobs für den Videoschnitt (Phase 5)."""

from __future__ import annotations

from app.config import get_settings
from app.db import session_scope
from app.models import Highlight
from app.services.activity import log_activity
from app.services.rendering import RenderingError, render_highlight
from app.services.streamers import RightsError
from app.workers.queues import QUEUE_MEDIA, make_job_id, safe_enqueue

JOBS = "app.workers.jobs.render"


def schedule_render(highlight_id: int, layout_choice: str = "auto", *, run: str | None = None) -> bool:
    return safe_enqueue(
        QUEUE_MEDIA,
        f"{JOBS}.render_highlight_job",
        highlight_id,
        layout_choice,
        job_id=make_job_id("render", highlight_id, *([run] if run else [])),
        timeout=3600,
    )


def render_highlight_job(highlight_id: int, layout_choice: str = "auto") -> dict:
    settings = get_settings()
    with session_scope() as db:
        highlight = db.get(Highlight, highlight_id)
        if highlight is None:
            return {"skipped": "fehlt"}
        try:
            video = render_highlight(db, settings, highlight, layout_choice=layout_choice)
        except (RenderingError, RightsError) as exc:
            if highlight.status == "rendering":
                highlight.status = "failed"
                highlight.error = f"Schnitt: {exc}"[:2000]
            log_activity(
                db, "error", "render", f"Highlight #{highlight_id}: {exc}", streamer_id=highlight.streamer_id
            )
            return {"error": str(exc)}
        return {"video": video.id, "layout": video.layout, "seconds": video.duration}
