"""Fertigstellung eines Highlights: Hochformat-Video mit Untertiteln, Überschrift und Metadaten."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import ActivityCategory, ActivityLevel, HighlightStatus, PublishStatus
from app.media.layout import choose_layout
from app.media.metadata import build_metadata
from app.media.render import RenderError, render_video
from app.media.subtitles import render_ass, to_srt, write_subtitles
from app.models import ClipRequest, Highlight, PublishJob, RenderedVideo, Streamer, StreamSession, User
from app.services.activity import log_activity
from app.services.media_probe import probe_media
from app.services.streamers import ensure_can_process
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

RENDERABLE = (
    HighlightStatus.CANDIDATE.value,
    HighlightStatus.RENDERED.value,
    HighlightStatus.FAILED.value,
    HighlightStatus.RENDERING.value,
)
LAYOUT_CHOICES = ("auto", "facecam_split", "blur_fill")


class RenderingError(RuntimeError):
    pass


def ensure_metadata(db: Session, settings: Settings, highlight: Highlight) -> None:
    """Titel, Beschreibung und Hashtags erzeugen – vom Redakteur bearbeitete Werte bleiben unangetastet."""
    if highlight.description:
        return
    streamer = db.get(Streamer, highlight.streamer_id)
    session = db.get(StreamSession, highlight.stream_session_id) if highlight.stream_session_id else None
    language = highlight.language or (settings.clip_language if settings.clip_language != "auto" else "de")
    meta = build_metadata(
        streamer_name=streamer.name,
        streamer_login=streamer.twitch_login,
        language=language[:2],
        source_title=highlight.title,
        words=(highlight.analysis or {}).get("words"),
        game=session.game_name if session else None,
        is_sample=streamer.is_demo,
    )
    highlight.title = meta.title
    highlight.description = meta.description
    highlight.hashtags = meta.hashtags
    db.flush()


def _protected_video_ids(db: Session, highlight: Highlight) -> set[int]:
    active = [s.value for s in (PublishStatus.PENDING_REVIEW, PublishStatus.APPROVED, PublishStatus.UPLOADING,
                                PublishStatus.PROCESSING)]  # fmt: skip
    return {
        job.rendered_video_id
        for job in db.query(PublishJob).filter(
            PublishJob.highlight_id == highlight.id, PublishJob.status.in_(active)
        )
        if job.rendered_video_id
    }


def render_highlight(
    db: Session,
    settings: Settings,
    highlight: Highlight,
    *,
    layout_choice: str = "auto",
    actor: User | None = None,
) -> RenderedVideo:
    if highlight.status not in RENDERABLE:
        raise RenderingError(f"Highlight im Status '{highlight.status}' wird nicht gerendert")
    streamer = db.get(Streamer, highlight.streamer_id)
    ensure_can_process(streamer)
    request = db.get(ClipRequest, highlight.clip_request_id) if highlight.clip_request_id else None
    source = Path(highlight.source_file_path or (request.file_path if request else "") or "")
    if not source.is_file():
        raise RenderingError("Quelldatei nicht (mehr) vorhanden – Aufbewahrungsfrist abgelaufen?")

    info = (request.media_info if request else None) or probe_media(source)
    if not info:
        raise RenderingError("Quelldatei ist kein lesbares Video")
    analysis = highlight.analysis or {}
    layout = choose_layout(
        int(info["width"]), int(info["height"]), analysis.get("facecam"),
        prefer=None if layout_choice == "auto" else layout_choice,
    )  # fmt: skip
    ensure_metadata(db, settings, highlight)

    highlight.status = HighlightStatus.RENDERING.value
    highlight.error = None
    db.flush()

    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    base = settings.renders_dir / str(streamer.id) / f"highlight-{highlight.id}-{stamp}"
    clip_duration = highlight.end_seconds - highlight.start_seconds
    words = analysis.get("words") or []
    subtitle_path = None
    if settings.subtitles_enabled:
        ass = render_ass(words if settings.subtitles_enabled else [], clip_duration, layout,
                         title=highlight.title, uppercase=settings.subtitles_uppercase)  # fmt: skip
        subtitle_path = write_subtitles(base.with_suffix(".ass"), ass)
        if words:
            write_subtitles(base.with_suffix(".srt"), to_srt(words, clip_duration))
    spikes = analysis.get("audio_spikes") or []
    try:
        result = render_video(
            source,
            base.with_suffix(".mp4"),
            start=highlight.start_seconds,
            end=highlight.end_seconds,
            layout=layout,
            subtitles=subtitle_path,
            has_audio=bool(info.get("has_audio", True)),
            thumbnail_at=(spikes[0] + 0.5) if spikes else None,
            preset=settings.render_preset,
            crf=settings.render_crf,
        )
    except RenderError as exc:
        highlight.status = HighlightStatus.FAILED.value
        highlight.error = f"Schnitt fehlgeschlagen: {exc}"[:2000]
        db.flush()
        raise RenderingError(str(exc)) from exc

    # Ältere Fassungen entfernen (Speicher sparen), sofern sie nicht in der TikTok-Warteschlange stecken
    protected = _protected_video_ids(db, highlight)
    for old in highlight.rendered_videos:
        if old.deleted_at is None and old.id not in protected:
            for path in (old.file_path, old.thumbnail_path, old.subtitle_path):
                if path:
                    Path(path).unlink(missing_ok=True)
            Path(old.file_path).with_suffix(".srt").unlink(missing_ok=True)
            old.deleted_at = utcnow()

    video = RenderedVideo(
        highlight_id=highlight.id,
        file_path=str(result.path),
        thumbnail_path=str(result.thumbnail),
        subtitle_path=str(subtitle_path) if subtitle_path else None,
        width=result.width,
        height=result.height,
        duration=result.duration,
        size_bytes=result.size_bytes,
        sha256=result.sha256,
        layout=layout.name,
    )
    db.add(video)
    highlight.status = HighlightStatus.RENDERED.value
    db.flush()
    db.refresh(highlight)
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.RENDER,
        f"Highlight #{highlight.id} geschnitten ({layout.name}, {result.duration:.0f} s, "
        f"{result.size_bytes / 1024**2:.1f} MB{', mit Untertiteln' if words else ''})",
        user_id=actor.id if actor else None,
        streamer_id=highlight.streamer_id,
    )
    return video


def update_metadata(
    db: Session, highlight: Highlight, *, title: str, description: str, hashtags: str, actor: User
) -> None:
    """Vom Redakteur bearbeitete Texte übernehmen (max. 5 Hashtags, Längen begrenzt)."""
    from app.media.metadata import MAX_TITLE, normalize_hashtags

    title = " ".join(title.split())
    if not title:
        raise RenderingError("Die Überschrift darf nicht leer sein.")
    if len(title) > MAX_TITLE:
        raise RenderingError(
            f"Die Überschrift darf höchstens {MAX_TITLE} Zeichen haben (wird im Video eingeblendet)."
        )
    description = description.strip()
    if len(description) > 2000:
        raise RenderingError("Die Beschreibung darf höchstens 2000 Zeichen haben.")
    highlight.title = title
    highlight.description = description or title
    highlight.hashtags = normalize_hashtags(hashtags)
    db.flush()
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.RENDER,
        f"Texte von Highlight #{highlight.id} bearbeitet",
        user_id=actor.id,
        streamer_id=highlight.streamer_id,
    )
