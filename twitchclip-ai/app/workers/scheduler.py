"""Periodischer Scheduler: reiht wiederkehrende Jobs ein.

Mehrere Scheduler-Instanzen sind unkritisch: Jede Ausführung wird per Redis-Lock
(SET NX EX) genau einmal pro Intervall eingereiht.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import time
from dataclasses import dataclass

from redis import Redis
from redis.exceptions import RedisError
from rq.scheduler import RQScheduler

from app.config import Settings, get_settings
from app.logging_setup import configure_logging
from app.redis_client import get_redis
from app.workers.queues import ALL_QUEUES, QUEUE_DEFAULT, enqueue_unique, get_queue, make_job_id

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
ALERTS_JOB = "app.workers.jobs.maintenance.alerts_job"

TWITCH_JOBS = "app.workers.jobs.twitch"


def build_periodic_jobs(settings: Settings) -> tuple[PeriodicJob, ...]:
    """Alle periodischen Jobs für die aktuelle Konfiguration."""
    jobs = list(PERIODIC_JOBS)
    jobs.append(PeriodicJob("alerts", ALERTS_JOB, settings.alert_check_interval_minutes * 60, timeout=120))
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
            PeriodicJob("chat_flush", "app.workers.jobs.analysis.flush_chat_job", 60),
        ]
    if settings.tiktok_configured and not settings.test_mode:
        from app.workers.jobs.publish import PERIODIC as TIKTOK_PERIODIC

        jobs += [
            PeriodicJob(name, func, interval, queue=queue) for name, func, interval, queue in TIKTOK_PERIODIC
        ]
    else:
        # Freigegebene Exporte auch ohne TikTok-Zugang zuverlässig abarbeiten
        jobs.append(PeriodicJob("tiktok_sync_publish", "app.workers.jobs.publish.sync_publish_jobs_job", 600))
    jobs.append(PeriodicJob("analysis_pending", "app.workers.jobs.analysis.analyze_pending_job", 600))
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


class DelayedJobPromoter:
    """Reiht fällige verzögerte RQ-Jobs (enqueue_in: Statusabfragen, Wiederholungen) in ihre Queues ein.

    Die Worker starten zwar ebenfalls einen RQ-Scheduler, versuchen die Sperre nach einem Neustart aber nur
    alle 10 Minuten erneut – hält der beendete Vorgänger-Container sie noch, blieben verzögerte Jobs bis
    zu ~20 Minuten liegen. Dieser Dienst versucht es bei jedem Tick (5 s); die RQ-Sperre je Queue
    verhindert doppeltes Einreihen, egal wer sie hält.
    """

    def __init__(self, redis: Redis) -> None:
        self.scheduler = RQScheduler([get_queue(name, redis) for name in ALL_QUEUES], connection=redis)

    def tick(self) -> set[str]:
        locks = self.scheduler.acquire_locks()
        if locks:
            self.scheduler.prepare_registries(locks)
            self.scheduler.enqueue_scheduled_jobs()
        return locks

    def release(self) -> None:
        with contextlib.suppress(RedisError):
            self.scheduler.release_locks()


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
    promoter = DelayedJobPromoter(redis)
    backoff = TICK_SECONDS
    while not stop.requested:
        try:
            promoter.tick()
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
    promoter.release()


if __name__ == "__main__":
    run_forever()
