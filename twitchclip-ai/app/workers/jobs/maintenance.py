"""Wartungsjobs: Speicherbereinigung, Protokoll-Rotation."""

from __future__ import annotations

import logging

from app.config import get_settings
from app.db import session_scope
from app.enums import ActivityCategory, ActivityLevel
from app.services.activity import log_activity, prune_activity
from app.services.storage import cleanup_storage, ensure_directories

logger = logging.getLogger(__name__)


def cleanup_storage_job(dry_run: bool = False) -> dict:
    settings = get_settings()
    ensure_directories(settings)
    with session_scope() as db:
        report = cleanup_storage(settings, db, dry_run=dry_run)
        if report.deleted_files or report.errors:
            log_activity(
                db,
                ActivityLevel.ERROR if report.errors else ActivityLevel.INFO,
                ActivityCategory.SYSTEM,
                f"Speicherbereinigung: {report.deleted_files} Dateien gelöscht, {report.freed_mb} MB frei"
                + (" (Probelauf)" if dry_run else ""),
                details={"errors": report.errors[:20], "purged_videos": report.purged_videos},
            )
    return {
        "deleted_files": report.deleted_files,
        "freed_mb": report.freed_mb,
        "purged_videos": report.purged_videos,
        "errors": report.errors,
    }


def prune_activity_job() -> int:
    settings = get_settings()
    with session_scope() as db:
        removed = prune_activity(db, settings.activity_retention_days)
        if removed:
            logger.info("Aktivitätsprotokoll bereinigt", extra={"removed": removed})
    return removed
