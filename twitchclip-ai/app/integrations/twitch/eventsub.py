"""EventSub-Webhooks: Signaturprüfung, Replay-Schutz und Nachrichtenmodell.

Twitch signiert jede Nachricht mit HMAC-SHA256 über  Message-Id + Timestamp + Rohdaten des Bodys.
Nachrichten älter als 10 Minuten werden verworfen; jede Message-Id wird nur einmal verarbeitet.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from redis import Redis
from redis.exceptions import RedisError

from app.integrations.twitch.client import parse_twitch_time
from app.timeutil import utcnow

HEADER_ID = "twitch-eventsub-message-id"
HEADER_TIMESTAMP = "twitch-eventsub-message-timestamp"
HEADER_SIGNATURE = "twitch-eventsub-message-signature"
HEADER_TYPE = "twitch-eventsub-message-type"
HEADER_SUBSCRIPTION_TYPE = "twitch-eventsub-subscription-type"

TYPE_VERIFICATION = "webhook_callback_verification"
TYPE_NOTIFICATION = "notification"
TYPE_REVOCATION = "revocation"

MAX_MESSAGE_AGE = timedelta(minutes=10)
DEDUP_TTL_SECONDS = 20 * 60
DEDUP_PREFIX = "tcai:eventsub:msg:"

SUBSCRIBED_TYPES = ("stream.online", "stream.offline")


class EventSubVerificationError(ValueError):
    """Nachricht ist nicht authentisch oder zu alt."""


@dataclass(frozen=True)
class EventSubMessage:
    message_id: str
    message_type: str
    timestamp: datetime
    subscription_id: str
    subscription_type: str
    subscription_status: str
    event: dict[str, Any]
    challenge: str | None
    raw: dict[str, Any]


def compute_signature(secret: str, message_id: str, timestamp: str, body: bytes) -> str:
    message = message_id.encode() + timestamp.encode() + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_and_parse(
    headers: dict[str, str],
    body: bytes,
    secret: str,
    *,
    now: datetime | None = None,
) -> EventSubMessage:
    """Prüft Signatur und Alter. Wirft EventSubVerificationError bei jeder Unstimmigkeit."""
    lowered = {k.lower(): v for k, v in headers.items()}
    message_id = lowered.get(HEADER_ID)
    timestamp = lowered.get(HEADER_TIMESTAMP)
    signature = lowered.get(HEADER_SIGNATURE)
    message_type = lowered.get(HEADER_TYPE)
    if not (message_id and timestamp and signature and message_type):
        raise EventSubVerificationError("EventSub-Header fehlen")
    if not secret:
        raise EventSubVerificationError("TWITCH_EVENTSUB_SECRET ist nicht gesetzt")

    expected = compute_signature(secret, message_id, timestamp, body)
    if not hmac.compare_digest(expected, signature):
        raise EventSubVerificationError("Ungültige Signatur")

    try:
        sent_at = parse_twitch_time(timestamp)
    except ValueError as exc:
        raise EventSubVerificationError("Ungültiger Zeitstempel") from exc
    if sent_at is None:
        raise EventSubVerificationError("Ungültiger Zeitstempel")
    current = now or utcnow()
    if current - sent_at > MAX_MESSAGE_AGE or sent_at - current > timedelta(minutes=2):
        raise EventSubVerificationError("Nachricht zu alt oder Zeitstempel in der Zukunft")

    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise EventSubVerificationError("Body ist kein JSON") from exc
    subscription = payload.get("subscription") or {}
    return EventSubMessage(
        message_id=message_id,
        message_type=message_type,
        timestamp=sent_at,
        subscription_id=str(subscription.get("id", "")),
        subscription_type=str(subscription.get("type") or lowered.get(HEADER_SUBSCRIPTION_TYPE, "")),
        subscription_status=str(subscription.get("status", "")),
        event=payload.get("event") or {},
        challenge=payload.get("challenge"),
        raw=payload,
    )


def is_duplicate(redis: Redis, message_id: str) -> bool:
    """True, wenn diese Message-Id bereits verarbeitet wurde. Markiert sie andernfalls atomar."""
    try:
        return not redis.set(DEDUP_PREFIX + message_id, "1", nx=True, ex=DEDUP_TTL_SECONDS)
    except RedisError:
        # Ohne Redis lieber verarbeiten: alle Handler sind idempotent.
        return False
