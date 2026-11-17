"""Speicherverwaltung: Verzeichnisse, Belegung und automatische Löschung alter Videodateien."""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import PROTECTED_PUBLISH_STATUSES
from app.models import PublishJob, RenderedVideo
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

GB = 1024**3


@dataclass
class CleanupReport:
    deleted_files: int = 0
    freed_bytes: int = 0
    purged_videos: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False
    deleted_paths: list[str] = field(default_factory=list)

    @property
    def freed_mb(self) -> float:
        return round(self.freed_bytes / 1024**2, 1)


def ensure_directories(settings: Settings) -> None:
    for directory in settings.managed_dirs():
        directory.mkdir(parents=True, exist_ok=True)


def is_within(path: Path, base: Path) -> bool:
    """Schutz gegen Path-Traversal: liegt path (aufgelöst) innerhalb von base?"""
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def disk_usage(path: Path) -> dict[str, float]:
    target = path if path.exists() else Path("/")
    usage = shutil.disk_usage(target)
    return {
        "total_gb": round(usage.total / GB, 2),
        "used_gb": round(usage.used / GB, 2),
        "free_gb": round(usage.free / GB, 2),
        "percent_used": round(usage.used / usage.total * 100, 1) if usage.total else 0.0,
    }


def _iter_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return [p for p in directory.rglob("*") if p.is_file() and not p.is_symlink()]


def _protected_paths(db: Session) -> set[str]:
    """Videos, die noch in der Freigabe-/Upload-Pipeline stecken, werden nie gelöscht."""
    rows = db.execute(
        select(RenderedVideo.file_path, RenderedVideo.thumbnail_path, RenderedVideo.subtitle_path)
        .join(PublishJob, PublishJob.rendered_video_id == RenderedVideo.id)
        .where(PublishJob.status.in_([s.value for s in PROTECTED_PUBLISH_STATUSES]))
    ).all()
    protected: set[str] = set()
    for row in rows:
        for value in row:
            if value:
                protected.add(str(Path(value).resolve()))
    return protected


def _delete(path: Path, report: CleanupReport) -> None:
    try:
        size = path.stat().st_size
        if not report.dry_run:
            path.unlink()
        report.deleted_files += 1
        report.freed_bytes += size
        report.deleted_paths.append(str(path))
    except FileNotFoundError:
        pass
    except OSError as exc:
        report.errors.append(f"{path}: {exc}")


def _remove_empty_dirs(directory: Path, older_than: float) -> None:
    """Entfernt leere Unterordner, die seit `older_than` (Unix-Zeit) unverändert sind.

    Die Altersgrenze verhindert, dass ein gerade von einem Job angelegter Ordner verschwindet.
    """
    if not directory.exists():
        return
    for sub in sorted(
        (p for p in directory.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True
    ):
        try:
            if sub.stat().st_mtime < older_than:
                sub.rmdir()
        except OSError:
            continue


def _mark_purged_videos(db: Session, deleted: set[str], now: datetime, report: CleanupReport) -> None:
    if not deleted:
        return
    videos = db.scalars(select(RenderedVideo).where(RenderedVideo.deleted_at.is_(None))).all()
    for video in videos:
        if str(Path(video.file_path).resolve()) in deleted:
            video.deleted_at = now
            report.purged_videos += 1


def cleanup_storage(
    settings: Settings, db: Session, *, now: datetime | None = None, dry_run: bool = False
) -> CleanupReport:
    """Löscht Dateien, die älter als STORAGE_RETENTION_DAYS sind, und erzwingt STORAGE_MAX_GB.

    Reihenfolge:
    1. tmp/ – alles älter als 6 Stunden (Zwischenstände abgebrochener Jobs)
    2. downloads/, renders/, exports/ – älter als STORAGE_RETENTION_DAYS (außer geschützte Videos)
    3. Wenn danach noch über STORAGE_MAX_GB: älteste ungeschützte Dateien zuerst löschen
    Beispieldateien in samples/ werden nie automatisch gelöscht.
    """
    now = now or utcnow()
    report = CleanupReport(dry_run=dry_run)
    protected = _protected_paths(db)
    retention_cutoff = (now - timedelta(days=settings.storage_retention_days)).timestamp()
    tmp_cutoff = (now - timedelta(hours=6)).timestamp()

    def is_protected(path: Path) -> bool:
        return str(path.resolve()) in protected

    for path in _iter_files(settings.tmp_dir):
        if path.stat().st_mtime < tmp_cutoff:
            _delete(path, report)

    for directory in (settings.downloads_dir, settings.renders_dir, settings.exports_dir):
        for path in _iter_files(directory):
            if is_protected(path):
                continue
            if path.stat().st_mtime < retention_cutoff:
                _delete(path, report)

    # Harte Obergrenze
    limit_bytes = int(settings.storage_max_gb * GB)
    managed = [settings.tmp_dir, settings.downloads_dir, settings.renders_dir, settings.exports_dir]
    deleted_now = {str(Path(p).resolve()) for p in report.deleted_paths}
    candidates: list[tuple[float, Path, int]] = []
    current = 0
    for directory in managed:
        for path in _iter_files(directory):
            if str(path.resolve()) in deleted_now:
                continue
            stat = path.stat()
            current += stat.st_size
            if not is_protected(path):
                candidates.append((stat.st_mtime, path, stat.st_size))
    if current > limit_bytes:
        # tmp und downloads vor fertigen Renderings opfern, jeweils älteste zuerst
        priority = {
            settings.tmp_dir: 0,
            settings.downloads_dir: 1,
            settings.exports_dir: 2,
            settings.renders_dir: 3,
        }

        def sort_key(item: tuple[float, Path, int]) -> tuple[int, float]:
            for base, rank in priority.items():
                if is_within(item[1], base):
                    return rank, item[0]
            return 9, item[0]

        for _mtime, path, size in sorted(candidates, key=sort_key):
            if current <= limit_bytes:
                break
            _delete(path, report)
            current -= size
        if current > limit_bytes:
            report.errors.append(
                f"Speicherlimit {settings.storage_max_gb} GB überschritten, "
                "aber verbleibende Dateien sind durch offene Freigaben geschützt."
            )

    if not dry_run:
        _mark_purged_videos(db, {str(Path(p).resolve()) for p in report.deleted_paths}, now, report)
        _remove_empty_dirs(settings.tmp_dir, tmp_cutoff)
        _remove_empty_dirs(settings.downloads_dir, tmp_cutoff)
    return report
