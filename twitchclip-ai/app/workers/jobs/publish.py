"""RQ-Jobs der TikTok-Veröffentlichung (Phase 6).

Ablauf eines freigegebenen Auftrags:
  approved ──(Rechte + Creator-Info prüfen, init)──> uploading ──(Stücke hochladen)──> processing
  processing ──(status/fetch mit Backoff)──> published | sent_to_inbox | failed

Doppelte Veröffentlichung ist ausgeschlossen:
- Der Auftrag wird atomar von „approved“ auf „uploading“ gesetzt (nur ein Worker gewinnt).
- Sobald TikTok eine publish_id vergeben hat, wird nie automatisch neu initialisiert.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx
from sqlalchemy import select, update

from app.config import get_settings
from app.db import session_scope
from app.enums import ActivityCategory, ActivityLevel, PublishMethod, PublishStatus
from app.integrations.tiktok.errors import (
    TikTokAPIError,
    TikTokAuthError,
    TikTokError,
    TikTokRateLimitedError,
)
from app.models import PublishJob, TikTokAccount
from app.services import publishing, tiktok_accounts
from app.services.activity import log_activity
from app.services.streamers import RightsError
from app.timeutil import utcnow
from app.workers.queues import QUEUE_DEFAULT, QUEUE_PUBLISH, make_job_id, safe_enqueue

logger = logging.getLogger(__name__)

JOBS = "app.workers.jobs.publish"
MAX_INIT_ATTEMPTS = 4
INIT_RETRY_DELAYS = (60, 300, 900)
UPLOAD_ATTEMPTS = 3
STATUS_GIVE_UP = timedelta(hours=48)
STALE_CLAIM = timedelta(minutes=30)
MIN_CHECK_GAP = timedelta(seconds=10)


def _http() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(30.0, write=300.0), follow_redirects=False)


def schedule_publish(job_id: int, *, delay_seconds: float | None = None, run: str | None = None) -> bool:
    return safe_enqueue(
        QUEUE_PUBLISH,
        f"{JOBS}.publish_job",
        job_id,
        job_id=make_job_id("publish", job_id, *([run] if run else [])),
        timeout=1800,
        delay_seconds=delay_seconds,
    )


def schedule_status_check(job_id: int, delay_seconds: float, *, seq: int | None = None) -> bool:
    """seq: laufende Nummer der Folgeabfrage. Pflicht, wenn eine Abfrage die nächste plant – sonst hätte
    die neue dieselbe Job-ID wie die gerade laufende und würde von enqueue_unique verworfen."""
    return safe_enqueue(
        QUEUE_PUBLISH,
        f"{JOBS}.check_status_job",
        job_id,
        job_id=make_job_id("tiktok-status", job_id, *([seq] if seq is not None else [])),
        timeout=120,
        delay_seconds=delay_seconds,
    )


def next_status_delay(job: PublishJob) -> int:
    """Backoff anhand der Zeit seit der Freigabe: zuerst alle 15 s, später stündlich."""
    started = job.approved_at or job.created_at
    elapsed = (utcnow() - started).total_seconds()
    for delay in publishing.STATUS_CHECK_DELAYS:
        if elapsed < delay * 4:
            return delay
    return publishing.STATUS_CHECK_DELAYS[-1]


def _claim(db, job_id: int) -> bool:
    """approved → uploading, atomar. Gibt False zurück, wenn ein anderer Worker schneller war."""
    result = db.execute(
        update(PublishJob)
        .where(PublishJob.id == job_id, PublishJob.status == PublishStatus.APPROVED.value)
        .values(status=PublishStatus.UPLOADING.value, updated_at=utcnow())
    )
    db.commit()
    return result.rowcount == 1


def _release(db, job: PublishJob, reason: str, delay: int) -> None:
    """Vor der Initialisierung gescheitert (z. B. Rate-Limit) → zurück auf approved, später erneut."""
    job.status = PublishStatus.APPROVED.value
    job.fail_reason = reason[:2000]
    db.commit()
    schedule_publish(job.id, delay_seconds=delay, run=f"retry{job.attempts}")


def publish_job(job_id: int) -> dict:
    settings = get_settings()
    with session_scope() as db:
        job = db.get(PublishJob, job_id)
        if job is None:
            return {"skipped": "fehlt"}
        if job.approved_at is None or not job.consent_confirmed:
            return {"skipped": "nicht freigegeben"}

        if job.method == PublishMethod.MANUAL_EXPORT:
            if job.status != PublishStatus.APPROVED:
                return {"skipped": job.status}
            try:
                publishing.check_rights_before_upload(db, job)
                path = publishing.create_export(db, settings, job)
            except (RightsError, publishing.PublishError) as exc:
                publishing._fail(db, job, str(exc))
                return {"error": str(exc)}
            return {"exported": str(path)}

        if not _claim(db, job_id):
            return {"skipped": "bereits in Bearbeitung"}
        db.refresh(job)

        # Schritt 1: Rechte, Creator-Info und Initialisierung
        try:
            publishing.check_rights_before_upload(db, job)
            with _http() as http:
                init = publishing.initialize_upload(db, settings, http, job)
            db.commit()  # publish_id sofort sichern – ab hier nie erneut initialisieren
        except RightsError as exc:
            publishing._fail(db, job, f"Rechteprüfung vor dem Upload: {exc}")
            return {"error": "rights"}
        except publishing.PublishError as exc:
            publishing._fail(db, job, str(exc))
            return {"error": str(exc)}
        except TikTokAuthError as exc:
            publishing._fail(db, job, f"TikTok-Konto muss neu verbunden werden ({exc}).")
            return {"error": "auth"}
        except TikTokRateLimitedError as exc:
            db.rollback()
            job = db.get(PublishJob, job_id)
            _release(
                db, job, "TikTok-Ratenlimit – neuer Versuch folgt automatisch.", int(exc.retry_after) + 5
            )
            return {"retry": "rate_limited"}
        except TikTokAPIError as exc:
            db.rollback()
            job = db.get(PublishJob, job_id)
            job.attempts += 1
            if exc.retryable and job.attempts < MAX_INIT_ATTEMPTS:
                delay = INIT_RETRY_DELAYS[min(job.attempts - 1, len(INIT_RETRY_DELAYS) - 1)]
                _release(db, job, f"Vorübergehender TikTok-Fehler: {str(exc)}", delay)
                return {"retry": exc.code}
            publishing._fail(db, job, str(exc))
            return {"error": exc.code}
        except TikTokError as exc:
            publishing._fail(db, job, str(exc))
            return {"error": "tiktok"}

        # Schritt 2: Upload in Stücken (mehrere Versuche mit derselben Upload-URL)
        last_error: TikTokError | None = None
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            try:
                with _http() as http:
                    publishing.upload(db, settings, http, job, init)
                db.commit()
                last_error = None
                break
            except TikTokAPIError as exc:
                last_error = exc
                db.rollback()
                job = db.get(PublishJob, job_id)
                if not exc.retryable:
                    break
                logger.warning("Upload-Versuch fehlgeschlagen", extra={"job": job_id, "attempt": attempt})
            except TikTokAuthError as exc:
                last_error = exc
                break
        if last_error is not None:
            publishing._fail(db, job, f"Upload abgebrochen: {last_error}. Erneut vorbereiten und freigeben.")
            return {"error": "upload"}

        schedule_status_check(job.id, publishing.STATUS_CHECK_DELAYS[0], seq=1)
        return {"publish_id": job.publish_id, "status": job.status}


def check_status_job(job_id: int) -> dict:
    settings = get_settings()
    with session_scope() as db:
        job = db.get(PublishJob, job_id)
        if job is None or not job.publish_id:
            return {"skipped": "kein publish_id"}
        if job.status not in (PublishStatus.UPLOADING, PublishStatus.PROCESSING):
            return {"skipped": job.status}
        if job.last_checked_at and utcnow() - job.last_checked_at < MIN_CHECK_GAP:
            # Eine zweite Abfragekette (z. B. durch den Abgleich) endet hier – es bleibt genau eine.
            return {"skipped": "gerade geprüft"}
        seq = int(utcnow().timestamp())
        account = db.get(TikTokAccount, job.tiktok_account_id) if job.tiktok_account_id else None
        if account is None:
            publishing._fail(db, job, "Das TikTok-Konto wurde entfernt – Status nicht mehr abrufbar.")
            return {"error": "account"}
        try:
            with _http() as http:
                status = tiktok_accounts.client_for(db, settings, http, account).fetch_status(job.publish_id)
        except TikTokRateLimitedError as exc:
            job.next_check_at = utcnow() + timedelta(seconds=exc.retry_after + 5)
            schedule_status_check(job.id, exc.retry_after + 5, seq=seq)
            return {"retry": "rate_limited"}
        except TikTokAuthError as exc:
            job.last_checked_at = utcnow()
            log_activity(
                db,
                ActivityLevel.WARNING,
                ActivityCategory.TIKTOK,
                f"Status von Auftrag #{job.id} nicht abrufbar: {exc}",
            )
            return {"error": "auth"}
        except TikTokAPIError as exc:
            job.last_checked_at = utcnow()
            if not exc.retryable:
                publishing._fail(db, job, str(exc))
                return {"error": exc.code}
            delay = next_status_delay(job)
            job.next_check_at = utcnow() + timedelta(seconds=delay)
            schedule_status_check(job.id, delay, seq=seq)
            return {"retry": exc.code}

        if publishing.apply_status(db, job, status, account):
            return {"final": job.status, "tiktok": status.status}
        if utcnow() - (job.approved_at or job.created_at) > STATUS_GIVE_UP:
            publishing._fail(
                db, job, f"TikTok hat die Verarbeitung nach 48 Stunden nicht abgeschlossen ({status.status})."
            )
            return {"error": "timeout"}
        delay = next_status_delay(job)
        job.next_check_at = utcnow() + timedelta(seconds=delay)
        db.flush()
        schedule_status_check(job.id, delay, seq=seq)
        return {"pending": status.status, "next_in": delay}


def sync_publish_jobs_job() -> dict:
    """Alle 10 Minuten: verlorene Jobs wieder aufnehmen (z. B. nach Redis-Neustart)."""
    result = {"publish": 0, "status": 0, "released": 0}
    now = utcnow()
    with session_scope() as db:
        jobs = db.scalars(
            select(PublishJob).where(
                PublishJob.status.in_(
                    [
                        PublishStatus.APPROVED.value,
                        PublishStatus.UPLOADING.value,
                        PublishStatus.PROCESSING.value,
                    ]
                )
            )
        ).all()
        for job in jobs:
            if job.status == PublishStatus.APPROVED:
                if schedule_publish(job.id):
                    result["publish"] += 1
            elif job.publish_id:
                due = job.next_check_at is None or job.next_check_at <= now
                if due and schedule_status_check(job.id, 1):
                    result["status"] += 1
            elif job.updated_at and now - job.updated_at > STALE_CLAIM:
                # Worker ist vor der Initialisierung abgestürzt → gefahrlos erneut versuchen
                job.status = PublishStatus.APPROVED.value
                result["released"] += 1
    return result


def refresh_tiktok_tokens_job() -> dict:
    settings = get_settings()
    if not settings.tiktok_configured:
        return {"skipped": "nicht konfiguriert"}
    with session_scope() as db, _http() as http:
        return tiktok_accounts.refresh_expiring_tokens(db, settings, http)


PERIODIC = (
    ("tiktok_sync_publish", f"{JOBS}.sync_publish_jobs_job", 600, QUEUE_PUBLISH),
    ("tiktok_refresh_tokens", f"{JOBS}.refresh_tiktok_tokens_job", 3600, QUEUE_DEFAULT),
)
