"""Suche nach öffentlichen Clips (Modus A): nur Metadaten, kein Download."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import ClipStatus
from app.integrations.twitch.client import HelixClient, TwitchClipData
from app.models import Streamer, TwitchClip
from app.services.clip_scoring import ClipFeatures, score_clips
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

# Twitch liefert bei langen Zeiträumen nicht zuverlässig alle Clips → in Fenstern abfragen.
SLICE_HOURS = 12
MAX_PAGES_PER_SLICE = 3


@dataclass
class DiscoveryReport:
    fetched: int = 0
    new: int = 0
    updated: int = 0
    recommended: int = 0


def time_slices(start: datetime, end: datetime, hours: int = SLICE_HOURS) -> list[tuple[datetime, datetime]]:
    slices = []
    cursor = start
    step = timedelta(hours=hours)
    while cursor < end:
        upper = min(end, cursor + step)
        slices.append((cursor, upper))
        cursor = upper
    return slices


def _apply(row: TwitchClip, data: TwitchClipData, now: datetime) -> None:
    row.url = data.url[:512]
    row.embed_url = (data.embed_url or "")[:512] or None
    row.thumbnail_url = (data.thumbnail_url or "")[:512] or None
    row.title = data.title[:300]
    row.creator_name = (data.creator_name or "")[:64] or None
    row.game_id = data.game_id
    row.language = (data.language or "")[:8] or None
    row.view_count = data.view_count
    row.duration = data.duration
    row.video_id = data.video_id
    row.vod_offset = data.vod_offset
    row.is_featured = data.is_featured
    row.clip_created_at = data.created_at
    row.last_seen_at = now


def discover_clips_for_streamer(
    db: Session, client: HelixClient, settings: Settings, streamer: Streamer, *, now: datetime | None = None
) -> DiscoveryReport:
    now = now or utcnow()
    report = DiscoveryReport()
    if not streamer.twitch_user_id:
        return report
    start = now - timedelta(hours=settings.clip_discovery_lookback_hours)

    fetched: dict[str, TwitchClipData] = {}
    for lower, upper in time_slices(start, now):
        for clip in client.get_clips(streamer.twitch_user_id, lower, upper, max_pages=MAX_PAGES_PER_SLICE):
            fetched[clip.id] = clip
    report.fetched = len(fetched)

    existing = (
        {
            row.twitch_clip_id: row
            for row in db.scalars(
                select(TwitchClip).where(TwitchClip.twitch_clip_id.in_(list(fetched)))
            ).all()
        }
        if fetched
        else {}
    )
    for clip_id, data in fetched.items():
        row = existing.get(clip_id)
        if row is None:
            row = TwitchClip(streamer_id=streamer.id, twitch_clip_id=clip_id, first_seen_at=now)
            db.add(row)
            report.new += 1
        else:
            report.updated += 1
        _apply(row, data, now)
    db.flush()

    # Alle Clips des Kanals im Betrachtungszeitraum gemeinsam (relativ) bewerten
    rows = db.scalars(
        select(TwitchClip).where(TwitchClip.streamer_id == streamer.id, TwitchClip.clip_created_at >= start)
    ).all()
    scores = score_clips(
        [
            ClipFeatures(
                key=row.twitch_clip_id,
                view_count=row.view_count,
                created_at=row.clip_created_at,
                duration=row.duration,
                is_featured=row.is_featured,
                video_id=row.video_id,
                vod_offset=row.vod_offset,
            )
            for row in rows
        ],
        now=now,
        duration_min=settings.clip_duration_min,
        duration_max=settings.clip_duration_max,
    )
    for row in rows:
        score, breakdown = scores[row.twitch_clip_id]
        row.discovery_score = score
        row.score_breakdown = breakdown
        if row.status != ClipStatus.DISMISSED:
            row.status = (
                ClipStatus.RECOMMENDED.value
                if score >= settings.min_highlight_score
                else ClipStatus.NEW.value
            )
            if row.status == ClipStatus.RECOMMENDED:
                report.recommended += 1
    streamer.last_clip_scan_at = now
    db.flush()
    return report


def prune_old_clips(db: Session, retention_days: int, *, now: datetime | None = None) -> int:
    cutoff = (now or utcnow()) - timedelta(days=retention_days)
    result = db.execute(delete(TwitchClip).where(TwitchClip.clip_created_at < cutoff))
    return int(result.rowcount or 0)
