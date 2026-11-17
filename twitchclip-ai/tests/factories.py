"""Kleine Helfer zum Anlegen von Testdaten."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

from app.models import Highlight, PublishJob, RenderedVideo, Streamer, TwitchClip
from app.timeutil import utcnow


def streamer(db, login: str = "kanal", **kwargs) -> Streamer:
    obj = Streamer(twitch_login=login, **kwargs)
    db.add(obj)
    db.flush()
    return obj


def highlight(
    db, streamer_obj: Streamer, score: float = 75.0, status: str = "candidate", **kwargs
) -> Highlight:
    ref = kwargs.pop("source_ref", f"sample-{score}-{status}-{utcnow().timestamp()}")
    obj = Highlight(
        streamer_id=streamer_obj.id,
        source_type="sample",
        source_ref=ref,
        start_seconds=10,
        end_seconds=40,
        score=score,
        score_breakdown={"audio": 0.8, "chat": 0.6},
        title=kwargs.pop("title", "Unglaublicher Moment"),
        description="Beschreibung",
        hashtags=["twitch", "gaming"],
        transcript="Das war knapp!",
        content_hash=hashlib.sha256(ref.encode()).hexdigest(),
        status=status,
        **kwargs,
    )
    db.add(obj)
    db.flush()
    return obj


def rendered_video(db, highlight_obj: Highlight, path: Path, thumb: Path | None = None) -> RenderedVideo:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 2048)
    if thumb is not None and not thumb.exists():
        thumb.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)
    obj = RenderedVideo(
        highlight_id=highlight_obj.id,
        file_path=str(path),
        thumbnail_path=str(thumb) if thumb else None,
        duration=30,
        size_bytes=path.stat().st_size,
    )
    db.add(obj)
    db.flush()
    return obj


def publish_job(
    db, highlight_obj: Highlight, video: RenderedVideo | None, status: str = "pending_review"
) -> PublishJob:
    obj = PublishJob(
        highlight_id=highlight_obj.id,
        rendered_video_id=video.id if video else None,
        caption="Caption #twitch",
        hashtags=["twitch"],
        status=status,
    )
    db.add(obj)
    db.flush()
    return obj


def twitch_clip(
    db, streamer_obj: Streamer, clip_id: str = "AwkwardHelplessSalamanderSwiftRage", **kwargs
) -> TwitchClip:
    obj = TwitchClip(
        streamer_id=streamer_obj.id,
        twitch_clip_id=clip_id,
        url=f"https://clips.twitch.tv/{clip_id}",
        embed_url=f"https://clips.twitch.tv/embed?clip={clip_id}",
        thumbnail_url="https://static-cdn.jtvnw.net/twitch-clips/abc/preview-480x272.jpg",
        title=kwargs.pop("title", "Epischer Clip"),
        creator_name="zuschauer",
        view_count=kwargs.pop("view_count", 1234),
        duration=28.5,
        clip_created_at=kwargs.pop("clip_created_at", utcnow() - timedelta(hours=3)),
        discovery_score=kwargs.pop("discovery_score", 81.0),
        **kwargs,
    )
    db.add(obj)
    db.flush()
    return obj
