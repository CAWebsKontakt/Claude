"""Automatische Streamüberwachung: Twitch-IDs auflösen, Live-Status, EventSub-Abos, VOD-Zuordnung.

Alle Funktionen sind idempotent: EventSub und Polling dürfen dieselbe Information mehrfach und in
beliebiger Reihenfolge liefern, ohne doppelte Streams oder flackernden Status zu erzeugen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import ActivityCategory, ActivityLevel, StreamStatus
from app.integrations.twitch.client import HelixClient, TwitchStream
from app.integrations.twitch.errors import EventSubConflictError, TwitchAPIError
from app.integrations.twitch.eventsub import SUBSCRIBED_TYPES
from app.models import EventSubSubscription, Streamer, StreamSession
from app.services.activity import log_activity
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

STATUS_KEY = "tcai:twitch:status"
# Nach einem stream.online-Ereignis kann Get Streams den Stream noch kurz nicht liefern.
ONLINE_GRACE = timedelta(minutes=5)
HEALTHY_SUB_STATUSES = {"enabled", "webhook_callback_verification_pending"}
REVOKE_TYPE = "user.authorization.revoke"
CHAT_TYPE = "channel.chat.message"
MANAGED_BROADCASTER_TYPES = (*SUBSCRIBED_TYPES, CHAT_TYPE)
CHAT_SCOPES_REQUIRED = {"user:read:chat", "user:bot"}


def _chat_reader(streamer: Streamer) -> str | None:
    """Twitch-Nutzer, der für den Kanal mitlesen darf: der Kanalinhaber mit Chat-Scopes (Modus B)."""
    if streamer.mode != "authorized":
        return None
    for token in streamer.oauth_tokens:
        if (
            token.provider == "twitch"
            and token.revoked_at is None
            and token.external_user_id == streamer.twitch_user_id
            and set(token.scopes or []) >= CHAT_SCOPES_REQUIRED
        ):
            return token.external_user_id
    return None


# ---------------------------------------------------------------------------
# Status für das Dashboard (Redis-Hash)
# ---------------------------------------------------------------------------
def record_status(redis: Redis, **fields: object) -> None:
    try:
        redis.hset(STATUS_KEY, mapping={k: "" if v is None else str(v) for k, v in fields.items()})
    except RedisError:
        logger.debug("Twitch-Status konnte nicht gespeichert werden", exc_info=True)


def read_status(redis: Redis) -> dict[str, str]:
    try:
        raw = redis.hgetall(STATUS_KEY)
    except RedisError:
        return {}
    return {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in raw.items()
    }


# ---------------------------------------------------------------------------
# Streamer auflösen
# ---------------------------------------------------------------------------
@dataclass
class ResolveReport:
    resolved: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)


def resolve_streamers(db: Session, client: HelixClient, *, refresh_known: bool = False) -> ResolveReport:
    """Ermittelt Twitch-IDs, Anzeigenamen, Profilbilder und Sprache.

    refresh_known=True aktualisiert auch bereits bekannte Kanäle (z. B. umbenannte Logins).
    """
    report = ResolveReport()
    pending = db.scalars(select(Streamer).where(Streamer.twitch_user_id.is_(None))).all()
    if pending:
        users = {u.login: u for u in client.get_users(logins=[s.twitch_login for s in pending])}
        for streamer in pending:
            user = users.get(streamer.twitch_login)
            if user is None:
                message = f"Twitch-Konto '{streamer.twitch_login}' nicht gefunden (Tippfehler oder gesperrt?)"
                if streamer.last_error != message:
                    streamer.last_error = message
                    log_activity(
                        db, ActivityLevel.WARNING, ActivityCategory.TWITCH, message, streamer_id=streamer.id
                    )
                report.not_found.append(streamer.twitch_login)
                continue
            clash = db.scalar(
                select(Streamer).where(Streamer.twitch_user_id == user.id, Streamer.id != streamer.id)
            )
            if clash is not None:
                streamer.last_error = f"Twitch-ID {user.id} ist bereits '{clash.twitch_login}' zugeordnet"
                report.not_found.append(streamer.twitch_login)
                continue
            _apply_user(streamer, user)
            report.resolved.append(streamer.twitch_login)
            log_activity(
                db,
                ActivityLevel.INFO,
                ActivityCategory.TWITCH,
                f"Twitch-Konto '{streamer.twitch_login}' verknüpft (ID {user.id})",
                streamer_id=streamer.id,
            )

    if refresh_known:
        known = db.scalars(select(Streamer).where(Streamer.twitch_user_id.is_not(None))).all()
        if known:
            users = {u.id: u for u in client.get_users(ids=[s.twitch_user_id for s in known])}
            for streamer in known:
                user = users.get(streamer.twitch_user_id)
                if user is None:
                    continue
                update_login = True
                if user.login != streamer.twitch_login:
                    collision = db.scalar(
                        select(Streamer).where(
                            Streamer.twitch_login == user.login, Streamer.id != streamer.id
                        )
                    )
                    update_login = collision is None
                    if update_login:
                        report.renamed.append((streamer.twitch_login, user.login))
                        log_activity(
                            db,
                            ActivityLevel.INFO,
                            ActivityCategory.TWITCH,
                            f"Twitch-Login geändert: '{streamer.twitch_login}' → '{user.login}'",
                            streamer_id=streamer.id,
                        )
                _apply_user(streamer, user, update_login=update_login)

    ids = [s.twitch_user_id for s in db.scalars(select(Streamer).where(Streamer.twitch_user_id.is_not(None)))]
    if ids and (report.resolved or refresh_known):
        languages = client.get_channel_languages(ids)
        for streamer in db.scalars(select(Streamer).where(Streamer.twitch_user_id.in_(list(languages)))):
            streamer.broadcaster_language = languages[streamer.twitch_user_id][:8]
    db.flush()
    return report


def _apply_user(streamer: Streamer, user, *, update_login: bool = True) -> None:
    streamer.twitch_user_id = user.id
    if update_login:
        streamer.twitch_login = user.login
    streamer.display_name = user.display_name[:64]
    streamer.profile_image_url = user.profile_image_url
    streamer.last_error = None


# ---------------------------------------------------------------------------
# Live-Status
# ---------------------------------------------------------------------------
def _open_sessions(db: Session, streamer: Streamer) -> list[StreamSession]:
    return list(
        db.scalars(
            select(StreamSession).where(
                StreamSession.streamer_id == streamer.id, StreamSession.ended_at.is_(None)
            )
        ).all()
    )


def apply_stream_online(
    db: Session,
    streamer: Streamer,
    *,
    stream_id: str | None,
    started_at: datetime | None,
    title: str | None = None,
    game_name: str | None = None,
    viewer_count: int | None = None,
    source: str,
    now: datetime | None = None,
) -> bool:
    """Markiert den Streamer als live. Gibt True zurück, wenn er vorher offline war."""
    now = now or utcnow()
    session = None
    if stream_id:
        session = db.scalar(select(StreamSession).where(StreamSession.twitch_stream_id == stream_id))
        if session is not None and session.ended_at is not None:
            # Veraltete Information zu einem bereits beendeten Stream (z. B. Cache von Get Streams)
            return False

    was_live = streamer.is_live
    streamer.is_live = True
    streamer.live_started_at = started_at or streamer.live_started_at or now
    if title is not None:
        streamer.live_title = title[:300]
    if game_name is not None:
        streamer.live_game_name = game_name[:200]
    if viewer_count is not None:
        streamer.live_viewer_count = viewer_count
    streamer.live_checked_at = now

    if session is None:
        for stale in _open_sessions(db, streamer):
            # Ein verpasstes Offline-Ereignis: alte Session schließen
            stale.ended_at = now
            stale.status = StreamStatus.ENDED.value
        session = StreamSession(
            streamer_id=streamer.id,
            twitch_stream_id=stream_id,
            title=(title or "")[:300] or None,
            game_name=(game_name or "")[:200] or None,
            started_at=streamer.live_started_at,
            status=StreamStatus.LIVE.value,
        )
        db.add(session)
    else:
        if title:
            session.title = title[:300]
        if game_name:
            session.game_name = game_name[:200]
    if viewer_count is not None:
        session.peak_viewers = max(session.peak_viewers or 0, viewer_count)
    db.flush()

    if not was_live:
        log_activity(
            db,
            ActivityLevel.INFO,
            ActivityCategory.TWITCH,
            f"{streamer.name} ist live" + (f": {title}" if title else "") + f" ({source})",
            streamer_id=streamer.id,
        )
        return True
    return False


def apply_stream_offline(
    db: Session, streamer: Streamer, *, source: str, now: datetime | None = None
) -> list[StreamSession]:
    """Markiert den Streamer als offline und schließt offene Sessions. Gibt die geschlossenen zurück."""
    now = now or utcnow()
    was_live = streamer.is_live
    streamer.is_live = False
    streamer.live_viewer_count = None
    streamer.live_checked_at = now
    closed = _open_sessions(db, streamer)
    for session in closed:
        session.ended_at = now
        session.status = StreamStatus.ENDED.value
    db.flush()
    if was_live or closed:
        log_activity(
            db,
            ActivityLevel.INFO,
            ActivityCategory.TWITCH,
            f"{streamer.name} ist offline ({source})",
            streamer_id=streamer.id,
        )
    return closed


@dataclass
class PollReport:
    checked: int = 0
    live: int = 0
    went_live: list[int] = field(default_factory=list)
    ended_sessions: list[int] = field(default_factory=list)
    ended_streamers: list[int] = field(default_factory=list)


def poll_live_status(
    db: Session,
    client: HelixClient,
    *,
    streamer_ids: list[int] | None = None,
    now: datetime | None = None,
) -> PollReport:
    now = now or utcnow()
    stmt = select(Streamer).where(Streamer.is_active.is_(True), Streamer.twitch_user_id.is_not(None))
    if streamer_ids:
        stmt = stmt.where(Streamer.id.in_(streamer_ids))
    streamers = list(db.scalars(stmt).all())
    report = PollReport(checked=len(streamers))
    if not streamers:
        return report
    streams: dict[str, TwitchStream] = {
        s.user_id: s for s in client.get_streams([st.twitch_user_id for st in streamers])
    }
    for streamer in streamers:
        stream = streams.get(streamer.twitch_user_id)
        if stream is not None:
            report.live += 1
            if apply_stream_online(
                db,
                streamer,
                stream_id=stream.id,
                started_at=stream.started_at,
                title=stream.title,
                game_name=stream.game_name,
                viewer_count=stream.viewer_count,
                source="Polling",
                now=now,
            ):
                report.went_live.append(streamer.id)
            continue
        streamer.live_checked_at = now
        if streamer.is_live:
            if streamer.live_started_at and now - streamer.live_started_at < ONLINE_GRACE:
                continue
            closed = apply_stream_offline(db, streamer, source="Polling", now=now)
            report.ended_streamers.append(streamer.id)
            report.ended_sessions.extend(s.id for s in closed)
    db.flush()
    return report


# ---------------------------------------------------------------------------
# VOD zu einem beendeten Stream finden
# ---------------------------------------------------------------------------
def attach_vod(db: Session, client: HelixClient, session: StreamSession) -> str | None:
    """Sucht das Archiv-VOD des Streams (Get Videos, type=archive, passendes stream_id)."""
    if session.vod_id:
        return session.vod_id
    streamer = db.get(Streamer, session.streamer_id)
    if streamer is None or not streamer.twitch_user_id:
        return None
    for video in client.get_videos(streamer.twitch_user_id, video_type="archive", first=20):
        if session.twitch_stream_id and video.stream_id == session.twitch_stream_id:
            session.vod_id = video.id
            db.flush()
            return video.id
    return None


# ---------------------------------------------------------------------------
# EventSub-Abgleich
# ---------------------------------------------------------------------------
@dataclass
class SyncReport:
    created: int = 0
    deleted: int = 0
    kept: int = 0
    errors: list[str] = field(default_factory=list)
    total_cost: int = 0
    max_total_cost: int = 0
    webhooks_active: bool = True


def sync_eventsub(db: Session, client: HelixClient, settings: Settings) -> SyncReport:
    """Stellt sicher, dass für jeden aktiven Streamer stream.online/offline abonniert ist – und sonst nichts.

    Alle Abos dieser Client-ID der verwalteten Typen gelten als „unsere“. Bitte die Twitch-App daher
    ausschließlich für TwitchClip AI verwenden.
    """
    report = SyncReport(webhooks_active=settings.eventsub_webhooks_active)
    listing = client.list_eventsub()
    report.total_cost = listing.total_cost
    report.max_total_cost = listing.max_total_cost
    callback = settings.eventsub_callback_url

    desired: dict[tuple[str, str], Streamer] = {}
    conditions: dict[tuple[str, str], dict[str, str]] = {}
    if report.webhooks_active:
        for streamer in db.scalars(
            select(Streamer).where(Streamer.is_active.is_(True), Streamer.twitch_user_id.is_not(None))
        ):
            for sub_type in SUBSCRIBED_TYPES:
                desired[(sub_type, streamer.twitch_user_id)] = streamer
            chat_user = _chat_reader(streamer) if settings.chat_stats_enabled else None
            if chat_user:
                key = (CHAT_TYPE, streamer.twitch_user_id)
                desired[key] = streamer
                conditions[key] = {"broadcaster_user_id": streamer.twitch_user_id, "user_id": chat_user}

    kept_remote: dict[tuple[str, str], object] = {}
    for sub in listing.subscriptions:
        if sub.type not in MANAGED_BROADCASTER_TYPES:
            continue
        key = (sub.type, sub.broadcaster_user_id or "")
        healthy = sub.status in HEALTHY_SUB_STATUSES and sub.callback == callback
        if key in desired and healthy and key not in kept_remote:
            kept_remote[key] = sub
            report.kept += 1
            continue
        try:
            client.delete_eventsub(sub.id)
            report.deleted += 1
            report.total_cost = max(0, report.total_cost - sub.cost)
        except TwitchAPIError as exc:
            report.errors.append(f"Löschen {sub.type} ({sub.broadcaster_user_id}): {exc}")

    secret = settings.twitch_eventsub_secret.get_secret_value()
    for key, streamer in desired.items():
        if key in kept_remote:
            continue
        if report.max_total_cost and report.total_cost + 1 > report.max_total_cost:
            report.errors.append("EventSub-Kostenlimit erreicht – weitere Kanäle nur per Polling")
            break
        sub_type, broadcaster_id = key
        try:
            info, totals = client.create_eventsub(
                sub_type, broadcaster_id, callback, secret, condition=conditions.get(key)
            )
            kept_remote[key] = info
            report.created += 1
            report.total_cost = totals["total_cost"] or report.total_cost + info.cost
            report.max_total_cost = totals["max_total_cost"] or report.max_total_cost
        except EventSubConflictError:
            report.kept += 1
        except TwitchAPIError as exc:
            report.errors.append(f"Anlegen {sub_type} für {streamer.twitch_login}: {exc}")

    _sync_revoke_subscription(client, settings, listing, report, secret)
    _mirror_subscriptions(db, kept_remote)
    db.flush()
    return report


def _sync_revoke_subscription(
    client: HelixClient, settings: Settings, listing, report: SyncReport, secret: str
) -> None:
    """Genau ein Abo user.authorization.revoke (Bedingung: unsere Client-ID), solange Webhooks aktiv sind.

    So erfährt das System sofort, wenn ein Streamer/Editor die App bei Twitch trennt.
    """
    existing = [s for s in listing.subscriptions if s.type == REVOKE_TYPE]
    keep = None
    for sub in existing:
        healthy = (
            report.webhooks_active
            and sub.status in HEALTHY_SUB_STATUSES
            and sub.callback == settings.eventsub_callback_url
            and sub.client_id == settings.twitch_client_id
        )
        if healthy and keep is None:
            keep = sub
            continue
        try:
            client.delete_eventsub(sub.id)
            report.deleted += 1
        except TwitchAPIError as exc:
            report.errors.append(f"Löschen {REVOKE_TYPE}: {exc}")
    if keep is not None:
        report.kept += 1
        return
    if not report.webhooks_active:
        return
    try:
        client.create_eventsub(
            REVOKE_TYPE,
            None,
            settings.eventsub_callback_url,
            secret,
            condition={"client_id": settings.twitch_client_id},
        )
        report.created += 1
    except EventSubConflictError:
        report.kept += 1
    except TwitchAPIError as exc:
        report.errors.append(f"Anlegen {REVOKE_TYPE}: {exc}")


def _mirror_subscriptions(db: Session, remote: dict[tuple[str, str], object]) -> None:
    """Spiegelt die gültigen Abos in die Tabelle eventsub_subscriptions (für das Dashboard)."""
    by_twitch_id = {
        s.twitch_user_id: s for s in db.scalars(select(Streamer).where(Streamer.twitch_user_id.is_not(None)))
    }
    remote_ids = set()
    for (_sub_type, broadcaster_id), sub in remote.items():
        streamer = by_twitch_id.get(broadcaster_id)
        if streamer is None:
            continue
        remote_ids.add(sub.id)
        row = db.scalar(
            select(EventSubSubscription).where(EventSubSubscription.twitch_subscription_id == sub.id)
        )
        if row is None:
            row = EventSubSubscription(
                streamer_id=streamer.id,
                twitch_subscription_id=sub.id,
                type=sub.type,
                version=sub.version,
                status=sub.status,
                cost=sub.cost,
            )
            db.add(row)
        else:
            row.status = sub.status
            row.cost = sub.cost
            row.streamer_id = streamer.id
    for row in db.scalars(select(EventSubSubscription)).all():
        if row.twitch_subscription_id not in remote_ids:
            db.delete(row)


def mark_subscription_status(db: Session, subscription_id: str, status: str) -> EventSubSubscription | None:
    row = db.scalar(
        select(EventSubSubscription).where(EventSubSubscription.twitch_subscription_id == subscription_id)
    )
    if row is not None:
        row.status = status
        db.flush()
    return row
