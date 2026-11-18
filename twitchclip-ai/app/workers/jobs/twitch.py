"""RQ-Jobs der Twitch-Überwachung (Phase 2).

Periodische Jobs fangen Twitch-Fehler ab und protokollieren sie gedrosselt – der nächste Lauf
versucht es ohnehin erneut. Ereignisgesteuerte Jobs (z. B. nach stream.offline) nutzen RQ-Retries.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import session_scope
from app.enums import ActivityCategory, ActivityLevel, StreamerMode
from app.integrations.twitch.client import HelixClient
from app.integrations.twitch.errors import TwitchError, TwitchRateLimitedError
from app.models import Streamer, StreamSession
from app.redis_client import get_redis
from app.services.activity import log_activity, log_activity_throttled
from app.services.clip_discovery import discover_clips_for_streamer, prune_old_clips
from app.services.streamers import compute_rights
from app.services.twitch_authorization import validate_all_tokens
from app.services.twitch_monitor import (
    attach_vod,
    poll_live_status,
    record_status,
    resolve_streamers,
    sync_eventsub,
)
from app.timeutil import utcnow
from app.workers.queues import QUEUE_DEFAULT, default_retry, make_job_id, safe_enqueue

logger = logging.getLogger(__name__)

JOBS = "app.workers.jobs.twitch"
VOD_LOOKUP_DELAY_SECONDS = 180
VOD_LOOKUP_ATTEMPTS = 4
CLIP_SCAN_AFTER_STREAM_SECONDS = 900


def twitch_available(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return settings.twitch_configured and not settings.test_mode


@contextmanager
def _client() -> Iterator[HelixClient]:
    with HelixClient(get_settings(), get_redis()) as client:
        yield client


def _report_error(kind: str, exc: Exception) -> None:
    redis = get_redis()
    record_status(redis, last_error=f"{kind}: {exc}", last_error_at=utcnow().isoformat())
    with session_scope() as db:
        log_activity_throttled(
            db,
            redis,
            f"twitch:{kind}:{exc.__class__.__name__}",
            1800,
            ActivityLevel.ERROR,
            ActivityCategory.TWITCH,
            f"Twitch-Fehler ({kind}): {exc}",
        )


def _skip(kind: str) -> dict:
    logger.debug("Twitch nicht konfiguriert oder Testmodus – übersprungen", extra={"job": kind})
    return {"skipped": True}


# ---------------------------------------------------------------------------
# Folgeaufgaben einreihen
# ---------------------------------------------------------------------------
def schedule_eventsub_sync(delay_seconds: float | None = None) -> bool:
    if not twitch_available():
        return False
    return safe_enqueue(
        QUEUE_DEFAULT, f"{JOBS}.sync_eventsub_job", job_id="twitch-eventsub_sync", delay_seconds=delay_seconds
    )


def schedule_streamer_refresh(streamer_id: int) -> bool:
    if not twitch_available():
        return False
    return safe_enqueue(
        QUEUE_DEFAULT,
        f"{JOBS}.refresh_streamer_job",
        streamer_id,
        job_id=make_job_id("twitch-refresh", streamer_id),
        retry=default_retry(),
    )


def schedule_stream_details(streamer_id: int, delay_seconds: float = 60) -> bool:
    if not twitch_available():
        return False
    return safe_enqueue(
        QUEUE_DEFAULT,
        f"{JOBS}.refresh_stream_details_job",
        streamer_id,
        job_id=make_job_id("twitch-details", streamer_id),
        delay_seconds=delay_seconds,
    )


def schedule_post_stream(session_ids: list[int], streamer_ids: list[int]) -> None:
    """Nach Streamende: VOD zuordnen und – sobald Zuschauer-Clips existieren – Clips suchen."""
    if not twitch_available():
        return
    for session_id in session_ids:
        safe_enqueue(
            QUEUE_DEFAULT,
            f"{JOBS}.attach_vod_job",
            session_id,
            1,
            job_id=make_job_id("twitch-vod", session_id),
            delay_seconds=VOD_LOOKUP_DELAY_SECONDS,
            retry=default_retry(),
        )
    for streamer_id in streamer_ids:
        safe_enqueue(
            QUEUE_DEFAULT,
            f"{JOBS}.discover_clips_job",
            streamer_id,
            job_id=make_job_id("twitch-clips-after-stream", streamer_id),
            delay_seconds=CLIP_SCAN_AFTER_STREAM_SECONDS,
        )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def resolve_streamers_job(refresh_known: bool = False) -> dict:
    if not twitch_available():
        return _skip("resolve")
    try:
        with _client() as client, session_scope() as db:
            report = resolve_streamers(db, client, refresh_known=refresh_known)
    except TwitchError as exc:
        _report_error("Streamer auflösen", exc)
        return {"error": str(exc)}
    record_status(get_redis(), last_resolve_at=utcnow().isoformat())
    if report.resolved or report.renamed:
        schedule_eventsub_sync()
    return {"resolved": report.resolved, "renamed": report.renamed, "not_found": report.not_found}


def resolve_streamers_job_refresh() -> dict:
    """Täglich: auch bekannte Kanäle aktualisieren (umbenannte Logins, Profilbilder, Sprache)."""
    return resolve_streamers_job(refresh_known=True)


def poll_live_status_job() -> dict:
    if not twitch_available():
        return _skip("poll")
    try:
        with _client() as client, session_scope() as db:
            report = poll_live_status(db, client)
    except TwitchError as exc:
        _report_error("Live-Status", exc)
        return {"error": str(exc)}
    record_status(
        get_redis(),
        last_poll_at=utcnow().isoformat(),
        last_poll_checked=report.checked,
        last_poll_live=report.live,
    )
    schedule_post_stream(report.ended_sessions, report.ended_streamers)
    return {"checked": report.checked, "live": report.live, "went_live": report.went_live}


def refresh_stream_details_job(streamer_id: int) -> dict:
    """Titel/Spiel/Zuschauer nach stream.online nachladen (EventSub liefert diese Daten nicht)."""
    if not twitch_available():
        return _skip("details")
    with _client() as client, session_scope() as db:
        report = poll_live_status(db, client, streamer_ids=[streamer_id])
    schedule_post_stream(report.ended_sessions, report.ended_streamers)
    return {"live": report.live}


def sync_eventsub_job() -> dict:
    if not twitch_available():
        return _skip("eventsub")
    settings = get_settings()
    try:
        with _client() as client, session_scope() as db:
            report = sync_eventsub(db, client, settings)
            if report.errors:
                log_activity_throttled(
                    db,
                    get_redis(),
                    "twitch:eventsub:errors",
                    3600,
                    ActivityLevel.ERROR,
                    ActivityCategory.TWITCH,
                    "EventSub-Abgleich mit Fehlern: " + "; ".join(report.errors[:5]),
                )
            elif report.created or report.deleted:
                log_activity(
                    db,
                    ActivityLevel.INFO,
                    ActivityCategory.TWITCH,
                    f"EventSub abgeglichen: {report.created} angelegt, {report.deleted} gelöscht, "
                    f"{report.kept} unverändert (Kosten {report.total_cost}/{report.max_total_cost})",
                )
    except TwitchError as exc:
        _report_error("EventSub", exc)
        return {"error": str(exc)}
    record_status(
        get_redis(),
        last_eventsub_sync_at=utcnow().isoformat(),
        eventsub_active=int(report.webhooks_active),
        eventsub_total_cost=report.total_cost,
        eventsub_max_total_cost=report.max_total_cost,
        eventsub_errors=len(report.errors),
    )
    return {
        "created": report.created,
        "deleted": report.deleted,
        "kept": report.kept,
        "errors": report.errors,
        "webhooks_active": report.webhooks_active,
    }


def discover_clips_job(streamer_id: int | None = None) -> dict:
    if not twitch_available():
        return _skip("clips")
    settings = get_settings()
    results: dict[str, dict] = {}
    with session_scope() as db:
        stmt = select(Streamer.id).where(Streamer.is_active.is_(True), Streamer.twitch_user_id.is_not(None))
        if streamer_id is not None:
            stmt = stmt.where(Streamer.id == streamer_id)
        ids = list(db.scalars(stmt.order_by(Streamer.last_clip_scan_at.asc().nulls_first())).all())
    with _client() as client:
        for sid in ids:
            try:
                with session_scope() as db:
                    streamer = db.get(Streamer, sid)
                    if streamer is None:
                        continue
                    report = discover_clips_for_streamer(db, client, settings, streamer)
                    results[streamer.twitch_login] = report.__dict__
            except TwitchRateLimitedError as exc:
                _report_error("Clip-Suche", exc)
                break  # Rest beim nächsten Lauf – die ältesten Scans kommen zuerst dran
            except TwitchError as exc:
                _report_error("Clip-Suche", exc)
    record_status(get_redis(), last_clip_scan_at=utcnow().isoformat())
    return results


def attach_vod_job(session_id: int, attempt: int = 1) -> dict:
    if not twitch_available():
        return _skip("vod")
    with _client() as client, session_scope() as db:
        session = db.get(StreamSession, session_id)
        if session is None:
            return {"missing": True}
        vod_id = attach_vod(db, client, session)
        streamer = db.get(Streamer, session.streamer_id)
        if vod_id:
            planned = False
            if (
                streamer is not None
                and streamer.mode == StreamerMode.AUTHORIZED
                and compute_rights(streamer).can_process
            ):
                from app.workers.jobs.clips import schedule_plan

                planned = schedule_plan(session_id)
            return {"vod_id": vod_id, "plan_scheduled": planned}
        if attempt < VOD_LOOKUP_ATTEMPTS:
            safe_enqueue(
                QUEUE_DEFAULT,
                f"{JOBS}.attach_vod_job",
                session_id,
                attempt + 1,
                job_id=make_job_id("twitch-vod", session_id, attempt + 1),
                delay_seconds=VOD_LOOKUP_DELAY_SECONDS * attempt,
            )
            return {"retry": attempt + 1}
        if streamer is not None and streamer.mode == StreamerMode.AUTHORIZED:
            log_activity(
                db,
                ActivityLevel.WARNING,
                ActivityCategory.TWITCH,
                f"Kein VOD für den Stream vom {session.started_at:%d.%m.%Y %H:%M} UTC gefunden – "
                "ist bei Twitch 'Vergangene Übertragungen speichern' aktiviert?",
                streamer_id=streamer.id,
            )
        return {"vod_id": None}


def refresh_streamer_job(streamer_id: int) -> dict:
    """Nach dem Hinzufügen oder per Knopfdruck: ID auflösen, Live-Status, Clips, EventSub."""
    if not twitch_available():
        return _skip("refresh")
    try:
        with _client() as client:
            with session_scope() as db:
                resolve_streamers(db, client)
            with session_scope() as db:
                report = poll_live_status(db, client, streamer_ids=[streamer_id])
            clips = None
            with session_scope() as db:
                streamer = db.get(Streamer, streamer_id)
                if streamer is not None and streamer.twitch_user_id:
                    clips = discover_clips_for_streamer(db, client, get_settings(), streamer).__dict__
    except TwitchError as exc:
        _report_error("Aktualisierung", exc)
        raise  # RQ wiederholt den Job mit Backoff
    schedule_post_stream(report.ended_sessions, report.ended_streamers)
    schedule_eventsub_sync()
    return {"live": report.live, "clips": clips}


def prune_twitch_clips_job() -> int:
    with session_scope() as db:
        return prune_old_clips(db, get_settings().clip_retention_days)


def validate_twitch_tokens_job() -> dict:
    """Stündlich: alle Nutzer-Tokens bei Twitch validieren (Vorgabe von Twitch), ggf. erneuern/sperren."""
    if not twitch_available():
        return _skip("validate")
    settings = get_settings()
    try:
        with _client() as client, session_scope() as db:
            report = validate_all_tokens(db, settings, client.http)
    except TwitchError as exc:
        _report_error("Token-Validierung", exc)
        return {"error": str(exc)}
    record_status(get_redis(), last_token_validation_at=utcnow().isoformat())
    return report.__dict__


def hype_scan_job() -> dict:
    """Hype-Liste aktualisieren (nur öffentliche Daten)."""
    from app.services.hype import scan

    settings = get_settings()
    if not twitch_available(settings) or not settings.hype_enabled:
        return {"skipped": True}
    try:
        with _client() as client, session_scope() as db:
            result = scan(db, settings, get_redis(), client)
    except TwitchRateLimitedError as exc:
        _report_error("Hype-Liste (Rate-Limit)", exc)
        return {"error": "rate_limited"}
    except TwitchError as exc:
        _report_error("Hype-Liste", exc)
        return {"error": str(exc)}
    return {"scanned": result.scanned, "in_range": result.in_range, "listed": len(result.entries)}
