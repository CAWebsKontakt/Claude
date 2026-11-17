"""Ausgabe geschnittener Videos und Vorschaubilder – nur für angemeldete Benutzer."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import ClipRequest, RenderedVideo, User
from app.services.storage import is_within
from app.web.deps import require_user

router = APIRouter(prefix="/media")


def _safe_file(settings: Settings, raw_path: str | None) -> Path:
    if not raw_path:
        raise HTTPException(status_code=404, detail="Datei nicht vorhanden")
    path = Path(raw_path)
    if not is_within(path, settings.data_dir) or not path.is_file():
        raise HTTPException(status_code=404, detail="Datei nicht vorhanden")
    return path


def _video(db: Session, video_id: int) -> RenderedVideo:
    video = db.get(RenderedVideo, video_id)
    if video is None or video.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Video nicht gefunden")
    return video


@router.get("/videos/{video_id}.mp4")
def video_file(
    video_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    video = _video(db, video_id)
    path = _safe_file(settings, video.file_path)
    # FileResponse unterstützt HTTP-Range-Requests (Spulen im mobilen Browser).
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/videos/{video_id}/download")
def video_download(
    video_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    video = _video(db, video_id)
    path = _safe_file(settings, video.file_path)
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"twitchclip-{video.highlight_id}-{video.id}.mp4",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/thumbnails/{video_id}.jpg")
def thumbnail(
    video_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    video = _video(db, video_id)
    path = _safe_file(settings, video.thumbnail_path)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/clips/{request_id}.mp4")
def clip_file(
    request_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Heruntergeladener Quell-Clip (Modus B) bzw. Beispieldatei (Testmodus)."""
    clip_request = db.get(ClipRequest, request_id)
    if clip_request is None:
        raise HTTPException(status_code=404, detail="Clip nicht gefunden")
    path = _safe_file(settings, clip_request.file_path)
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, max-age=3600"})
