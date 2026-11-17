"""RQ-Jobs der berechtigten Clip-Verarbeitung (Phase 3).

Wiederholungen werden hier selbst geplant (statt RQ-Retry), damit der Status jedes Auftrags in der
Datenbank jederzeit stimmt: „Twitch verarbeitet noch“ → erneuter Versuch mit wachsendem Abstand;
nach der letzten Wiederholung → „Fehlgeschlagen“ mit Begründung im Dashboard.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.enums import ClipRequestStatus, StreamerMode
from app.integrations.twitch.client import HelixClient
from app.integrations.twitch.errors import TwitchAPIError, TwitchAuthError, TwitchError
from app.models import ClipRequest, Streamer, StreamSession
from app.redis_client import get_redis
from app.services import processing_keys
from app.services.clip_discovery import discover_clips_for_streamer
from app.services.clip_processing import (
    ClipNotReadyError,
    ClipRequestError,
    create_clip,
    download_clip,
    ingest_samples,
    mark_failed,
    plan_viewer_clips,
)
from app.services.streamers import RightsError, compute_rights
from app.services.twitch_authorization import mark_revoked
from app.timeutil import utcnow
from app.workers.queues import QUEUE_DEFAULT, QUEUE_MEDIA, make_job_id, safe_enqueue

logger = logging.getLogger(__name__)

JOBS = "app.workers.jobs.clips"
# Abstände zwischen Wiederholungen (Sekunden); Länge = maximale Anzahl Versuche - 1
CREATE_RETRY_DELAYS = (60, 300)
DOWNLOAD_RETRY_DELAYS = (30, 60, 120, 300, 600, 1200)
PLAN_DELAY_AFTER_VOD_SECONDS = 900


def _twitch_ready() -> bool:
    settings = get_settings()
    return settings.twitch_configured and not settings.test_mode


# ---------------------------------------------------------------------------
# Einplanen
# ---------------------------------------------------------------------------
def schedule_plan(
    session_id: int, *, delay_seconds: float | None = PLAN_DELAY_AFTER_VOD_SECONDS, force: bool = False
) -> bool:
    if not _twitch_ready():
        return False
    return safe_enqueue(
        QUEUE_DEFAULT,
        f"{JOBS}.plan_stream_clips_job",
        session_id,
        force,
        job_id=make_job_id("clips-plan", session_id),
        delay_seconds=delay_seconds,
    )


def schedule_create(
    request_id: int, attempt: int = 1, delay_seconds: float | None = None, *, run: str | None = None
) -> bool:
    """run: eindeutige Kennung für manuelle Neustarts (unabhängig vom Versuchszähler)."""
    return safe_enqueue(
        QUEUE_DEFAULT,
        f"{JOBS}.create_clip_job",
        request_id,
        attempt,
        job_id=make_job_id("clips-create", request_id, *([run] if run else []), attempt),
        delay_seconds=delay_seconds,
    )


def schedule_download(
    request_id: int, attempt: int = 1, delay_seconds: float | None = None, *, run: str | None = None
) -> bool:
    """run: eindeutige Kennung für manuelle Neustarts (unabhängig vom Versuchszähler)."""
    return safe_enqueue(
        QUEUE_MEDIA,
        f"{JOBS}.download_clip_job",
        request_id,
        attempt,
        job_id=make_job_id("clips-download", request_id, *([run] if run else []), attempt),
        delay_seconds=delay_seconds,
    )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def plan_stream_clips_job(session_id: int, force: bool = False) -> dict:
    """Nach Streamende: Zuschauer-Clips aktualisieren und die besten als Aufträge einplanen."""
    if not _twitch_ready():
        return {"skipped": True}
    settings = get_settings()
    with session_scope() as db:
        session = db.get(StreamSession, session_id)
        if session is None or not session.vod_id:
            return {"skipped": "kein VOD"}
        streamer = db.get(Streamer, session.streamer_id)
        if (
            streamer is None
            or streamer.mode != StreamerMode.AUTHORIZED
            or not compute_rights(streamer).can_process
        ):
            return {"skipped": "keine Verarbeitungsrechte"}
        if not force and not processing_keys.claim(db, f"plan-session-{session_id}", "plan_stream_clips"):
            return {"skipped": "bereits geplant"}
    with HelixClient(settings, get_redis()) as client, session_scope() as db:
        streamer = db.get(Streamer, session.streamer_id)
        try:
            discover_clips_for_streamer(db, client, settings, streamer)
        except TwitchError as exc:
            logger.warning("Clip-Suche vor der Planung fehlgeschlagen", extra={"error": str(exc)})
        try:
            requests = plan_viewer_clips(db, settings, db.get(StreamSession, session_id))
        except RightsError:
            return {"skipped": "keine Verarbeitungsrechte"}
        ids = [r.id for r in requests]
    for request_id in ids:
        schedule_download(request_id)  # Zuschauer-Clips existieren bereits – direkt herunterladen
    return {"planned": ids}


def _handle_twitch_error(db, request: ClipRequest, exc: TwitchError) -> str:
    """Nicht wiederholbare Twitch-Fehler: Auftrag beenden; 403 heißt meist „kein Editor“."""
    if isinstance(exc, TwitchAuthError):
        request.status = ClipRequestStatus.SKIPPED.value
        request.error = "Twitch-Autorisierung ungültig – Streamer/Editor muss neu autorisieren"
        return "auth"
    if isinstance(exc, TwitchAPIError) and exc.status_code == 403:
        streamer = db.get(Streamer, request.streamer_id)
        token = next((t for t in streamer.oauth_tokens if t.id == request.oauth_token_id), None)
        if token is not None:
            mark_revoked(
                db, token, "Twitch verweigert den Clip-Zugriff (403) – ist das Konto Editor des Kanals?"
            )
        mark_failed(db, request, f"Zugriff verweigert: {exc}")
        return "forbidden"
    mark_failed(db, request, str(exc))
    return "failed"


def create_clip_job(request_id: int, attempt: int = 1) -> dict:
    if not _twitch_ready():
        return {"skipped": True}
    settings = get_settings()
    with HelixClient(settings, get_redis()) as client, session_scope() as db:
        request = db.get(ClipRequest, request_id)
        if request is None or request.status not in (ClipRequestStatus.PLANNED, ClipRequestStatus.CREATING):
            return {"skipped": "Status"}
        try:
            if not create_clip(db, settings, client, request):
                return {"skipped": "Rechte"}
        except TwitchError as exc:
            retryable = getattr(exc, "retryable", False)
            if retryable and attempt <= len(CREATE_RETRY_DELAYS):
                request.status = ClipRequestStatus.PLANNED.value
                request.error = f"Versuch {attempt}: {exc}"
                schedule_create(request_id, attempt + 1, CREATE_RETRY_DELAYS[attempt - 1])
                return {"retry": attempt + 1}
            return {"error": _handle_twitch_error(db, request, exc)}
    # Twitch braucht einige Sekunden bis Minuten, bis ein neuer Clip herunterladbar ist
    schedule_download(request_id, 1, delay_seconds=30)
    return {"created": True}


def download_clip_job(request_id: int, attempt: int = 1) -> dict:
    if not _twitch_ready():
        return {"skipped": True}
    settings = get_settings()
    with HelixClient(settings, get_redis()) as client, session_scope() as db:
        request = db.get(ClipRequest, request_id)
        if request is None or request.status in (
            ClipRequestStatus.DOWNLOADED,
            ClipRequestStatus.DUPLICATE,
            ClipRequestStatus.SKIPPED,
        ):
            return {"skipped": "Status"}
        try:
            result = download_clip(db, settings, client, request)
        except (ClipNotReadyError, TwitchError, OSError, ValueError, ClipRequestError) as exc:
            retryable = isinstance(exc, ClipNotReadyError | OSError) or getattr(exc, "retryable", False)
            if retryable and attempt <= len(DOWNLOAD_RETRY_DELAYS):
                request.status = ClipRequestStatus.WAITING.value
                request.error = f"Versuch {attempt}: {exc}"
                schedule_download(request_id, attempt + 1, DOWNLOAD_RETRY_DELAYS[attempt - 1])
                return {"retry": attempt + 1}
            if isinstance(exc, TwitchError):
                return {"error": _handle_twitch_error(db, request, exc)}
            mark_failed(db, request, str(exc))
            return {"error": str(exc)}
        if result is None:
            return {"skipped": "Rechte"}
        return {"downloaded": str(result.path), "duplicate_of": result.duplicate_of}


def ingest_samples_job() -> dict:
    """Testmodus: neue Dateien in /data/samples übernehmen."""
    settings = get_settings()
    if not settings.test_mode:
        return {"skipped": True}
    with session_scope() as db:
        created = ingest_samples(db, settings)
        return {"ingested": [r.id for r in created]}


def resume_stuck_requests_job() -> dict:
    """Aufträge, deren Job verloren ging (z. B. Redis-Neustart), wieder anstoßen."""
    if not _twitch_ready():
        return {"skipped": True}
    resumed = []
    # Länger als die größte geplante Wartezeit unverändert → es wartet sicher kein Job mehr darauf.
    cutoff = utcnow() - timedelta(hours=1)
    with session_scope() as db:
        rows = db.scalars(
            select(ClipRequest).where(
                ClipRequest.status.in_(
                    [
                        ClipRequestStatus.PLANNED.value,
                        ClipRequestStatus.WAITING.value,
                        ClipRequestStatus.CREATING.value,
                        ClipRequestStatus.DOWNLOADING.value,
                    ]
                ),
                ClipRequest.updated_at < cutoff,
                ClipRequest.source != "sample",
            )
        ).all()
        for row in rows:
            row.updated_at = utcnow()
            if row.twitch_clip_id:
                if schedule_download(row.id, row.attempts + 1):
                    resumed.append(row.id)
            elif schedule_create(row.id, row.attempts + 1):
                resumed.append(row.id)
    return {"resumed": resumed}
