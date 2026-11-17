"""Systemzustand für /readyz und die Systemseite im Dashboard."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from redis import Redis
from redis.exceptions import RedisError
from rq import Worker
from rq.registry import FailedJobRegistry, StartedJobRegistry
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.config import Settings
from app.services.storage import directory_size, disk_usage
from app.workers.queues import ALL_QUEUES, get_queue
from app.workers.scheduler import HEARTBEAT_KEY

logger = logging.getLogger(__name__)


@dataclass
class QueueInfo:
    name: str
    queued: int
    started: int
    failed: int


@dataclass
class SystemStatus:
    version: str
    database_ok: bool
    redis_ok: bool
    database_error: str | None = None
    redis_error: str | None = None
    workers: list[dict] = field(default_factory=list)
    queues: list[QueueInfo] = field(default_factory=list)
    scheduler_age_seconds: int | None = None
    disk: dict | None = None
    data_size_gb: float | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.database_ok and self.redis_ok


def check_database(db: Session) -> tuple[bool, str | None]:
    try:
        db.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, exc.__class__.__name__


def check_redis(redis: Redis) -> tuple[bool, str | None]:
    try:
        redis.ping()
        return True, None
    except RedisError as exc:
        return False, exc.__class__.__name__


def collect_status(settings: Settings, db: Session, redis: Redis, *, detailed: bool = True) -> SystemStatus:
    db_ok, db_err = check_database(db)
    redis_ok, redis_err = check_redis(redis)
    status = SystemStatus(
        version=__version__,
        database_ok=db_ok,
        redis_ok=redis_ok,
        database_error=db_err,
        redis_error=redis_err,
    )
    if not detailed:
        return status

    if redis_ok:
        try:
            for worker in Worker.all(connection=redis):
                status.workers.append(
                    {
                        "name": worker.name,
                        "queues": ", ".join(worker.queue_names()),
                        "state": worker.get_state(),
                        "last_heartbeat": worker.last_heartbeat,
                    }
                )
            for name in ALL_QUEUES:
                queue = get_queue(name, redis)
                status.queues.append(
                    QueueInfo(
                        name=name,
                        queued=queue.count,
                        started=StartedJobRegistry(queue=queue).count,
                        failed=FailedJobRegistry(queue=queue).count,
                    )
                )
            beat = redis.get(HEARTBEAT_KEY)
            if beat:
                status.scheduler_age_seconds = max(0, int(time.time()) - int(beat))
        except RedisError as exc:
            status.warnings.append(f"RQ-Status nicht lesbar: {exc.__class__.__name__}")

        if not status.workers:
            status.warnings.append("Kein RQ-Worker aktiv – Hintergrundaufgaben werden nicht abgearbeitet.")
        if status.scheduler_age_seconds is None or status.scheduler_age_seconds > 120:
            status.warnings.append("Scheduler sendet keinen Herzschlag – periodische Jobs laufen nicht.")
        for queue_info in status.queues:
            if queue_info.failed:
                status.warnings.append(
                    f"Queue '{queue_info.name}': {queue_info.failed} fehlgeschlagene Jobs."
                )

    try:
        status.disk = disk_usage(settings.data_dir)
        status.data_size_gb = round(sum(directory_size(d) for d in settings.managed_dirs()) / 1024**3, 2)
        if status.disk["free_gb"] < 5:
            status.warnings.append(f"Wenig freier Speicher: {status.disk['free_gb']} GB")
        if status.data_size_gb > settings.storage_max_gb:
            status.warnings.append(
                f"Mediendaten ({status.data_size_gb} GB) über STORAGE_MAX_GB ({settings.storage_max_gb} GB)."
            )
    except OSError as exc:
        status.warnings.append(f"Speicherbelegung nicht lesbar: {exc}")
    return status
