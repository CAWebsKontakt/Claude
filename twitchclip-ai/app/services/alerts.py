"""Alarmierung (Phase 7): Probleme erkennen und einmalig an einen Webhook melden.

Prüft System (Worker, Scheduler, Speicher, fehlgeschlagene Jobs) und Fachliches (Konten, die neu verbunden
werden müssen, auslaufende Erlaubnisse, fehlgeschlagene Uploads/Downloads). Jede Meldung wird höchstens alle
ALERT_REPEAT_HOURS wiederholt; verschwindet ein Problem, kommt einmalig eine Entwarnung.

Unterstützte Ziele (ALERT_WEBHOOK_URL): Discord- und Slack-Webhooks (JSON), alles andere als Klartext-POST
(z. B. ntfy.sh, Gotify-kompatible Proxys, eigene Endpunkte).

Ergänzend prüft scripts/healthcheck.sh per Cron von außen – das meldet auch, wenn der Scheduler selbst
(und damit diese Prüfung) ausgefallen ist.
"""

from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass
from datetime import timedelta

import httpx
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.enums import ActivityCategory, ActivityLevel, PublishStatus, RightsStatus, StreamerMode
from app.models import ClipRequest, PublishJob, Streamer, TikTokAccount
from app.services.activity import log_activity
from app.services.streamers import compute_rights
from app.services.system import collect_status
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

ACTIVE_KEY = "tcai:alerts:active"
SENT_PREFIX = "tcai:alerts:sent:"
LAST_RUN_KEY = "tcai:alerts:last-run"
PERMISSION_WARN_DAYS = 7
FAILED_CLIPS_THRESHOLD = 5


@dataclass(frozen=True)
class Alert:
    key: str
    level: str  # "warning" | "error"
    message: str


def collect_alerts(settings: Settings, db: Session, redis: Redis) -> list[Alert]:
    alerts: list[Alert] = []
    status = collect_status(settings, db, redis)
    if not status.database_ok:
        alerts.append(Alert("db", "error", f"Datenbank nicht erreichbar ({status.database_error})"))
    if not status.redis_ok:
        alerts.append(Alert("redis", "error", f"Redis nicht erreichbar ({status.redis_error})"))
    if status.redis_ok:
        if not status.workers:
            alerts.append(Alert("workers", "error", "Kein Worker aktiv – Hintergrundaufgaben stehen still."))
        if status.scheduler_age_seconds is None or status.scheduler_age_seconds > 180:
            alerts.append(Alert("scheduler", "error", "Scheduler sendet keinen Herzschlag."))
        for queue in status.queues:
            if queue.failed >= 10:
                alerts.append(
                    Alert(
                        f"queue-failed-{queue.name}",
                        "warning",
                        f"Queue „{queue.name}“: {queue.failed} fehlgeschlagene Jobs (Dashboard → System).",
                    )
                )
            if queue.queued >= 200:
                alerts.append(
                    Alert(
                        f"queue-backlog-{queue.name}",
                        "warning",
                        f"Queue „{queue.name}“: {queue.queued} wartende Jobs – Worker überlastet?",
                    )
                )
    if status.disk:
        if status.disk["free_gb"] < 5:
            alerts.append(Alert("disk", "error", f"Nur noch {status.disk['free_gb']} GB freier Speicher."))
        elif status.disk.get("percent_used", 0) >= 90:
            alerts.append(Alert("disk", "warning", f"Speicher zu {status.disk['percent_used']} % belegt."))
    if status.data_size_gb is not None and status.data_size_gb > settings.storage_max_gb:
        alerts.append(
            Alert(
                "media-size",
                "warning",
                f"Mediendaten {status.data_size_gb} GB > STORAGE_MAX_GB {settings.storage_max_gb} GB.",
            )
        )

    if status.database_ok:
        alerts.extend(_domain_alerts(settings, db))
    return alerts


def _domain_alerts(settings: Settings, db: Session) -> list[Alert]:
    alerts: list[Alert] = []
    now = utcnow()
    streamers = db.scalars(
        select(Streamer)
        .where(Streamer.mode == StreamerMode.AUTHORIZED.value, Streamer.is_demo.is_(False))
        .options(selectinload(Streamer.permissions), selectinload(Streamer.oauth_tokens))
    ).all()
    for streamer in streamers:
        rights = compute_rights(streamer, now)
        if rights.status == RightsStatus.MISSING_OAUTH:
            alerts.append(
                Alert(
                    f"twitch-oauth-{streamer.id}",
                    "warning",
                    f"{streamer.name}: Twitch-Autorisierung fehlt oder wurde widerrufen – neuen Link senden.",
                )
            )
        permission = rights.permission
        if permission is not None and permission.valid_until is not None:
            left = permission.valid_until - now
            if left < timedelta(days=PERMISSION_WARN_DAYS):
                alerts.append(
                    Alert(
                        f"permission-expiry-{permission.id}",
                        "warning",
                        f"{streamer.name}: Erlaubnis läuft am {permission.valid_until:%d.%m.%Y} ab.",
                    )
                )

    for account in db.scalars(
        select(TikTokAccount)
        .where(TikTokAccount.is_active.is_(True))
        .options(selectinload(TikTokAccount.oauth_tokens))
    ).all():
        if not any(t.revoked_at is None for t in account.oauth_tokens):
            alerts.append(
                Alert(
                    f"tiktok-account-{account.id}",
                    "warning",
                    f"TikTok-Konto „{account.display_name or account.open_id}“ muss neu verbunden werden.",
                )
            )

    day_ago = now - timedelta(hours=24)
    failed_jobs = db.scalars(
        select(PublishJob).where(
            PublishJob.status == PublishStatus.FAILED.value, PublishJob.updated_at >= day_ago
        )
    ).all()
    for job in failed_jobs:
        alerts.append(
            Alert(
                f"publish-failed-{job.id}",
                "error",
                f"TikTok-Auftrag #{job.id} fehlgeschlagen: {(job.fail_reason or '')[:200]}",
            )
        )
    stuck = db.scalar(
        select(func.count(PublishJob.id)).where(
            PublishJob.status == PublishStatus.PENDING_REVIEW.value,
            PublishJob.created_at < now - timedelta(days=3),
        )
    )
    if stuck:
        alerts.append(
            Alert(
                "publish-review-backlog",
                "warning",
                f"{stuck} Video(s) warten seit über 3 Tagen auf Freigabe.",
            )
        )

    failed_clips = db.scalar(
        select(func.count(ClipRequest.id)).where(
            ClipRequest.status == "failed", ClipRequest.updated_at >= day_ago
        )
    )
    if failed_clips and failed_clips >= FAILED_CLIPS_THRESHOLD:
        alerts.append(
            Alert(
                "clips-failed",
                "warning",
                f"{failed_clips} Clip-Aufträge in 24 h fehlgeschlagen (Aktivität & Fehler prüfen).",
            )
        )

    return alerts


def _payload(url: str, text: str) -> tuple[dict | None, str | None]:
    if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
        return {"content": text[:1900]}, None
    if "hooks.slack.com" in url:
        return {"text": text}, None
    return None, text


def send(settings: Settings, http: httpx.Client, text: str) -> bool:
    url = settings.alert_webhook_url
    if not url:
        return False
    body, plain = _payload(url, text)
    try:
        if body is not None:
            response = http.post(url, json=body, timeout=10)
        else:
            response = http.post(
                url,
                content=plain.encode(),
                headers={"Content-Type": "text/plain; charset=utf-8", "Title": "TwitchClip AI"},
                timeout=10,
            )
        response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning("Alarm konnte nicht gesendet werden", extra={"error": exc.__class__.__name__})
        return False


def _decode(values) -> set[str]:
    return {v.decode() if isinstance(v, bytes) else v for v in values}


def run_alerts(settings: Settings, db: Session, redis: Redis, http: httpx.Client) -> dict:
    """Ein Durchlauf: neue/fortbestehende Probleme melden (gedrosselt), behobene einmalig entwarnen."""
    alerts = collect_alerts(settings, db, redis)
    current = {a.key: a for a in alerts}
    host = settings.app_base_url or socket.gethostname()
    try:
        previous = _decode(redis.hkeys(ACTIVE_KEY))
    except RedisError:
        previous = set()
    due: list[Alert] = []
    for alert in alerts:
        try:
            fresh = redis.set(SENT_PREFIX + alert.key, "1", nx=True, ex=settings.alert_repeat_hours * 3600)
        except RedisError:
            fresh = alert.key not in previous
        if fresh:
            due.append(alert)
    resolved = sorted(previous - current.keys())

    sent = False
    if due or resolved:
        lines = [f"TwitchClip AI – {host}"]
        lines += [("🔴 " if a.level == "error" else "🟠 ") + a.message for a in due]
        if resolved:
            lines.append(f"✅ behoben: {len(resolved)} Meldung(en)")
        sent = send(settings, http, "\n".join(lines))
        for alert in due:
            if alert.level == "error":
                log_activity(db, ActivityLevel.ERROR, ActivityCategory.SYSTEM, f"Alarm: {alert.message}")
    try:
        pipe = redis.pipeline()
        pipe.delete(ACTIVE_KEY)
        if current:
            pipe.hset(ACTIVE_KEY, mapping={k: f"{a.level}|{a.message}" for k, a in current.items()})
        for key in resolved:
            pipe.delete(SENT_PREFIX + key)
        pipe.set(LAST_RUN_KEY, json.dumps({"at": utcnow().isoformat(), "active": len(current)}))
        pipe.execute()
    except RedisError:
        pass
    return {"active": len(current), "notified": len(due), "resolved": len(resolved), "sent": sent}


def active_alerts(redis: Redis) -> list[Alert]:
    """Ergebnis der letzten Prüfung (für Dashboard und Metriken)."""
    try:
        raw = redis.hgetall(ACTIVE_KEY)
    except RedisError:
        return []
    result = []
    for key, value in raw.items():
        key = key.decode() if isinstance(key, bytes) else key
        value = value.decode() if isinstance(value, bytes) else value
        level, _, message = value.partition("|")
        result.append(Alert(key, level, message))
    return sorted(result, key=lambda a: (a.level != "error", a.message))


def last_run(redis: Redis) -> dict | None:
    try:
        raw = redis.get(LAST_RUN_KEY)
        return json.loads(raw) if raw else None
    except (RedisError, ValueError):
        return None
