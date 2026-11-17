"""Periodischer Scheduler: reiht wiederkehrende Jobs ein.

Mehrere Scheduler-Instanzen sind unkritisch: Jede Ausführung wird per Redis-Lock
(SET NX EX) genau einmal pro Intervall eingereiht.
"""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass

from redis import Redis
from redis.exceptions import RedisError

from app.config import Settings, get_settings
from app.logging_setup import configure_logging
from app.redis_client import get_redis
from app.workers.queues import QUEUE_DEFAULT, enqueue_unique, make_job_id

logger = logging.getLogger("tcai.scheduler")

HEARTBEAT_KEY = "tcai:heartbeat:scheduler"
HEARTBEAT_TTL = 120
TICK_SECONDS = 5


@dataclass(frozen=True)
class PeriodicJob:
    name: str
    func: str
    interval_seconds: int
    queue: str = QUEUE_DEFAULT
    timeout: int | None = None


# Wartungsjobs – laufen immer.
PERIODIC_JOBS: tuple[PeriodicJob, ...] = (
    PeriodicJob("storage_cleanup", "app.workers.jobs.maintenance.cleanup_storage_job", 3600),
    PeriodicJob("prune_activity", "app.workers.jobs.maintenance.prune_activity_job", 86400),
)

TWITCH_JOBS = "app.workers.jobs.twitch"


def build_periodic_jobs(settings: Settings) -> tuple[PeriodicJob, ...]:
    """Alle periodischen Jobs für die aktuelle Konfiguration."""
    jobs = list(PERIODIC_JOBS)
    if settings.twitch_configured and not settings.test_mode:
        jobs += [
            PeriodicJob("twitch_resolve", f"{TWITCH_JOBS}.resolve_streamers_job", 600),
            PeriodicJob("twitch_refresh_known", f"{TWITCH_JOBS}.resolve_streamers_job_refresh", 86400),
            PeriodicJob(
                "twitch_poll_live",
                f"{TWITCH_JOBS}.poll_live_status_job",
                settings.twitch_poll_interval_seconds,
            ),
            PeriodicJob("twitch_eventsub_sync", f"{TWITCH_JOBS}.sync_eventsub_job", 6 * 3600),
            PeriodicJob(
                "twitch_discover_clips",
                f"{TWITCH_JOBS}.discover_clips_job",
                settings.clip_discovery_interval_minutes * 60,
                timeout=1800,
            ),
            PeriodicJob("twitch_prune_clips", f"{TWITCH_JOBS}.prune_twitch_clips_job", 86400),
            PeriodicJob("twitch_validate_tokens", f"{TWITCH_JOBS}.validate_twitch_tokens_job", 3600),
            PeriodicJob("clips_resume_stuck", "app.workers.jobs.clips.resume_stuck_requests_job", 1800),
        ]
    if settings.test_mode:
        jobs.append(PeriodicJob("samples_ingest", "app.workers.jobs.clips.ingest_samples_job", 300))
    return tuple(jobs)


def tick(redis: Redis, jobs: tuple[PeriodicJob, ...] = PERIODIC_JOBS, now: float | None = None) -> list[str]:
    """Ein Scheduler-Durchlauf. Gibt die Namen der neu eingereihten Jobs zurück."""
    now = now or time.time()
    redis.set(HEARTBEAT_KEY, str(int(now)), ex=HEARTBEAT_TTL)
    enqueued: list[str] = []
    for job in jobs:
        lock_key = f"tcai:sched:{job.name}"
        if not redis.set(lock_key, str(int(now)), nx=True, ex=job.interval_seconds):
            continue
        _job, created = enqueue_unique(
            job.queue,
            job.func,
            job_id=make_job_id("periodic", job.name),
            connection=redis,
            timeout=job.timeout,
        )
        if created:
            enqueued.append(job.name)
    return enqueued


class _Stop:
    requested = False


def run_forever() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    stop = _Stop()

    def _handle(signum: int, _frame: object) -> None:
        logger.info("Scheduler wird beendet", extra={"signal": signum})
        stop.requested = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    jobs = build_periodic_jobs(settings)
    logger.info("Scheduler gestartet", extra={"jobs": [j.name for j in jobs]})
    redis = get_redis()
    backoff = TICK_SECONDS
    while not stop.requested:
        try:
            names = tick(redis, jobs)
            if names:
                logger.info("Periodische Jobs eingereiht", extra={"jobs": names})
            backoff = TICK_SECONDS
        except RedisError as exc:
            logger.error("Redis-Fehler im Scheduler", extra={"error": str(exc)})
            backoff = min(backoff * 2, 60)
        except Exception:  # noqa: BLE001 – Scheduler darf nie abstürzen
            logger.exception("Unerwarteter Fehler im Scheduler")
            backoff = min(backoff * 2, 60)
        slept = 0.0
        while slept < backoff and not stop.requested:
            time.sleep(0.5)
            slept += 0.5


if __name__ == "__main__":
    run_forever()
