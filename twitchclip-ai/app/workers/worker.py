"""RQ-Worker-Einstiegspunkt.

Aufruf: python -m app.workers.worker default publish
        python -m app.workers.worker media
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import uuid

from rq import Worker

from app.config import get_settings
from app.logging_setup import configure_logging
from app.redis_client import get_redis
from app.services.storage import ensure_directories
from app.workers.queues import ALL_QUEUES, get_queue

logger = logging.getLogger("tcai.worker")


def main(argv: list[str] | None = None) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    names = argv if argv else ["default"]
    unknown = [n for n in names if n not in ALL_QUEUES]
    if unknown:
        raise SystemExit(f"Unbekannte Queue(s): {', '.join(unknown)}. Erlaubt: {', '.join(ALL_QUEUES)}")
    ensure_directories(settings)
    redis = get_redis()
    queues = [get_queue(name, redis) for name in names]
    worker = Worker(
        queues,
        connection=redis,
        # Eindeutiger Name: ein Neustart des Containers darf nicht mit der alten
        # (noch nicht abgelaufenen) Worker-Registrierung in Redis kollidieren.
        name=f"{socket.gethostname()}-{'-'.join(names)}-{os.getpid()}-{uuid.uuid4().hex[:6]}",
    )
    logger.info("Worker gestartet", extra={"queues": names})
    worker.work(with_scheduler=True, logging_level=settings.log_level)


if __name__ == "__main__":
    main(sys.argv[1:])
