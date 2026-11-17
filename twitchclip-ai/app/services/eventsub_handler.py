"""Verarbeitung eingehender EventSub-Nachrichten (stream.online, stream.offline, Widerruf).

Der Datenbankzustand wird direkt im Webhook-Request aktualisiert (schnell, idempotent).
Alles, was weitere API-Aufrufe braucht, wird als Folgeaufgabe zurückgegeben und nach dem
Commit in die Warteschlange gestellt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import ActivityCategory, ActivityLevel
from app.integrations.twitch.client import parse_twitch_time
from app.integrations.twitch.eventsub import TYPE_NOTIFICATION, TYPE_REVOCATION, EventSubMessage
from app.models import Streamer
from app.services.activity import log_activity
from app.services.twitch_monitor import apply_stream_offline, apply_stream_online, mark_subscription_status

logger = logging.getLogger(__name__)


@dataclass
class FollowUps:
    stream_details_for: list[int] = field(default_factory=list)
    ended_sessions: list[int] = field(default_factory=list)
    ended_streamers: list[int] = field(default_factory=list)
    resync_eventsub: bool = False


def handle_message(db: Session, message: EventSubMessage) -> FollowUps:
    follow = FollowUps()
    if message.message_type == TYPE_NOTIFICATION:
        _handle_notification(db, message, follow)
    elif message.message_type == TYPE_REVOCATION:
        row = mark_subscription_status(db, message.subscription_id, message.subscription_status)
        log_activity(
            db,
            ActivityLevel.WARNING,
            ActivityCategory.TWITCH,
            f"EventSub-Abo {message.subscription_type} von Twitch widerrufen "
            f"(Grund: {message.subscription_status}) – wird neu abgeglichen",
            streamer_id=row.streamer_id if row else None,
        )
        follow.resync_eventsub = True
    return follow


def _handle_notification(db: Session, message: EventSubMessage, follow: FollowUps) -> None:
    event = message.event
    broadcaster_id = str(event.get("broadcaster_user_id") or "")
    streamer = db.scalar(select(Streamer).where(Streamer.twitch_user_id == broadcaster_id))
    if streamer is None or not streamer.is_active:
        # Streamer entfernt/pausiert – der nächste EventSub-Abgleich löscht das Abo.
        logger.info(
            "EventSub-Ereignis für unbekannten/pausierten Kanal", extra={"broadcaster": broadcaster_id}
        )
        follow.resync_eventsub = True
        return

    if message.subscription_type == "stream.online":
        stream_type = event.get("type") or "live"
        apply_stream_online(
            db,
            streamer,
            stream_id=str(event.get("id") or "") or None,
            started_at=parse_twitch_time(event.get("started_at")),
            source="EventSub" if stream_type == "live" else f"EventSub, Typ {stream_type}",
        )
        follow.stream_details_for.append(streamer.id)
    elif message.subscription_type == "stream.offline":
        closed = apply_stream_offline(db, streamer, source="EventSub")
        follow.ended_sessions.extend(s.id for s in closed)
        follow.ended_streamers.append(streamer.id)
