"""Prometheus-Metriken (Phase 7). Nur aktiv mit METRICS_TOKEN; Abruf mit „Authorization: Bearer <token>“.

Beispiel (Prometheus scrape_config):
    - job_name: twitchclip
      scheme: https
      authorization: { credentials: "<METRICS_TOKEN>" }
      static_configs: [{ targets: ["clips.example.com"] }]
"""

from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import __version__
from app.config import Settings, get_settings
from app.db import get_db
from app.models import ClipRequest, Highlight, PublishJob, Streamer, TikTokAccount
from app.redis_client import get_redis
from app.services.alerts import ACTIVE_KEY, LAST_RUN_KEY
from app.services.system import collect_status

router = APIRouter()


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.declared: set[str] = set()

    def add(
        self, name: str, value: float | int | None, help_text: str, kind: str = "gauge", **labels
    ) -> None:
        if value is None:
            return
        if name not in self.declared:
            self.lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
            self.declared.add(name)
        label = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
        self.lines.append(f"{name}{{{label}}} {value}" if label else f"{name} {value}")

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"


def _authorize(request: Request, settings: Settings) -> None:
    token = settings.metrics_token.get_secret_value()
    if not token:
        raise HTTPException(status_code=404, detail="Nicht gefunden")
    header = request.headers.get("authorization", "")
    supplied = header[7:] if header.lower().startswith("bearer ") else ""
    if not supplied or not hmac.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(
            status_code=401, detail="Nicht autorisiert", headers={"WWW-Authenticate": "Bearer"}
        )


def _grouped(db: Session, column, model) -> dict[str, int]:
    return {
        str(k): int(v) for k, v in db.execute(select(column, func.count(model.id)).group_by(column)).all()
    }


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    _authorize(request, settings)
    redis = get_redis()
    status = collect_status(settings, db, redis)
    w = _Writer()
    w.add("tcai_info", 1, "Version und Modus", version=__version__, test_mode=str(settings.test_mode).lower())
    w.add("tcai_database_up", int(status.database_ok), "Datenbank erreichbar")
    w.add("tcai_redis_up", int(status.redis_ok), "Redis erreichbar")
    w.add("tcai_workers", len(status.workers), "Aktive RQ-Worker")
    w.add(
        "tcai_scheduler_heartbeat_age_seconds",
        status.scheduler_age_seconds,
        "Alter des Scheduler-Herzschlags",
    )
    for queue in status.queues:
        for state, value in (("queued", queue.queued), ("started", queue.started), ("failed", queue.failed)):
            w.add("tcai_queue_jobs", value, "Jobs je Queue und Zustand", queue=queue.name, state=state)
    if status.disk:
        w.add(
            "tcai_disk_free_bytes",
            int(status.disk["free_gb"] * 1024**3),
            "Freier Speicher im Datenverzeichnis",
        )
        w.add("tcai_disk_used_ratio", round(status.disk["percent_used"] / 100, 4), "Belegter Anteil")
    if status.data_size_gb is not None:
        w.add("tcai_media_bytes", int(status.data_size_gb * 1024**3), "Belegung durch Mediendaten")

    if status.database_ok:
        for mode, count in _grouped(db, Streamer.mode, Streamer).items():
            w.add("tcai_streamers", count, "Streamer je Modus", mode=mode)
        live = db.scalar(select(func.count(Streamer.id)).where(Streamer.is_live.is_(True)))
        w.add("tcai_streamers_live", live, "Streamer, die gerade live sind")
        for state, count in _grouped(db, Highlight.status, Highlight).items():
            w.add("tcai_highlights", count, "Highlights je Status", status=state)
        for state, count in _grouped(db, ClipRequest.status, ClipRequest).items():
            w.add("tcai_clip_requests", count, "Clip-Aufträge je Status", status=state)
        for state, count in _grouped(db, PublishJob.status, PublishJob).items():
            w.add("tcai_publish_jobs", count, "TikTok-Aufträge je Status", status=state)
        accounts = db.scalar(select(func.count(TikTokAccount.id)).where(TikTokAccount.is_active.is_(True)))
        w.add("tcai_tiktok_accounts", accounts, "Verbundene TikTok-Konten")

    if status.redis_ok:
        try:
            w.add("tcai_alerts_active", redis.hlen(ACTIVE_KEY), "Aktive Alarme")
            raw = redis.get(LAST_RUN_KEY)
            if raw:
                from datetime import datetime

                at = datetime.fromisoformat(json.loads(raw)["at"])
                w.add("tcai_alerts_last_run_timestamp_seconds", int(at.timestamp()), "Letzte Alarmprüfung")
        except (RedisError, ValueError, KeyError):
            pass
    return PlainTextResponse(w.render(), media_type="text/plain; version=0.0.4; charset=utf-8")
