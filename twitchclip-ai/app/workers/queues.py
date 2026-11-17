"""RQ-Warteschlangen und idempotentes Einreihen von Jobs."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from redis import Redis
from redis.exceptions import RedisError
from rq import Queue, Retry
from rq.exceptions import NoSuchJobError
from rq.job import Job, JobStatus

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

QUEUE_DEFAULT = "default"  # leichte API-Jobs: Twitch-Polling, Clip-Suche, Token-Refresh
QUEUE_MEDIA = "media"  # schwere Jobs: Download, Whisper, FFmpeg (Concurrency 1)
QUEUE_PUBLISH = "publish"  # TikTok-Uploads und Statusabfragen
ALL_QUEUES = (QUEUE_DEFAULT, QUEUE_MEDIA, QUEUE_PUBLISH)

ACTIVE_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.STARTED,
    JobStatus.SCHEDULED,
    JobStatus.DEFERRED,
    JobStatus.CREATED,
}

# RQ 3 erlaubt in Job-IDs nur Buchstaben, Ziffern, Unterstrich und Bindestrich.
_JOB_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def make_job_id(*parts: object) -> str:
    """Baut eine stabile, RQ-kompatible Job-ID, z. B. make_job_id("vod", 123, 4500) -> "vod-123-4500"."""
    return "-".join(_JOB_ID_UNSAFE.sub("_", str(p)) for p in parts)[:200]


DEFAULT_TIMEOUTS = {QUEUE_DEFAULT: 300, QUEUE_MEDIA: 3600, QUEUE_PUBLISH: 900}


def get_queue(name: str, connection: Redis | None = None) -> Queue:
    if name not in ALL_QUEUES:
        raise ValueError(f"Unbekannte Queue: {name}")
    return Queue(name, connection=connection or get_redis(), default_timeout=DEFAULT_TIMEOUTS[name])


def default_retry() -> Retry:
    # Exponentielles Backoff: 1 min, 5 min, 15 min
    return Retry(max=3, interval=[60, 300, 900])


def enqueue_unique(
    queue_name: str,
    func: Callable[..., Any] | str,
    *args: Any,
    job_id: str,
    connection: Redis | None = None,
    retry: Retry | None = None,
    timeout: int | None = None,
    delay_seconds: float | None = None,
    **kwargs: Any,
) -> tuple[Job, bool]:
    """Reiht einen Job nur ein, wenn kein Job mit gleicher ID aktiv ist.

    Gibt (Job, neu_eingereiht) zurück. Abgeschlossene/fehlgeschlagene Jobs mit derselben ID
    werden ersetzt, damit periodische Jobs erneut laufen können.
    """
    if _JOB_ID_UNSAFE.search(job_id):
        raise ValueError(f"Ungültige Job-ID '{job_id}' – make_job_id() verwenden")
    connection = connection or get_redis()
    queue = get_queue(queue_name, connection)
    try:
        existing = Job.fetch(job_id, connection=connection)
        status = existing.get_status(refresh=True)
        if status in ACTIVE_STATUSES:
            return existing, False
        existing.delete()
    except NoSuchJobError:
        pass
    options: dict[str, Any] = {
        "job_id": job_id,
        "retry": retry,
        "result_ttl": 3600,
        "failure_ttl": 7 * 86400,
    }
    if delay_seconds and delay_seconds > 0:
        # Verzögerte Jobs übernimmt der in jedem Worker laufende RQ-Scheduler (with_scheduler=True).
        job = queue.enqueue_in(
            timedelta(seconds=delay_seconds),
            func,
            args=args,
            kwargs=kwargs,
            job_timeout=timeout,
            **options,
        )
    else:
        job = queue.enqueue_call(func, args=args, kwargs=kwargs, timeout=timeout, **options)
    logger.info("Job eingereiht", extra={"job_id": job_id, "queue": queue_name})
    return job, True


def safe_enqueue(
    queue_name: str, func: Callable[..., Any] | str, *args: Any, job_id: str, **kwargs: Any
) -> bool:
    """Wie enqueue_unique, aber ein Redis-Ausfall bricht die aufrufende Aktion (z. B. Webhook) nicht ab."""
    try:
        _job, created = enqueue_unique(queue_name, func, *args, job_id=job_id, **kwargs)
        return created
    except RedisError:
        logger.error("Job konnte nicht eingereiht werden (Redis)", extra={"job_id": job_id})
        return False
