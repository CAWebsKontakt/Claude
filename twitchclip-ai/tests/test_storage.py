import os
import time
from datetime import timedelta

from app.models import RenderedVideo
from app.services.storage import cleanup_storage, ensure_directories, is_within
from app.timeutil import utcnow
from tests import factories


def _file(path, size=1024, age_days=0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    mtime = time.time() - age_days * 86400
    os.utime(path, (mtime, mtime))
    return path


def test_retention_deletes_old_files_only(settings, db_session):
    ensure_directories(settings)
    old_download = _file(
        settings.downloads_dir / "vod1" / "a.mp4", age_days=settings.storage_retention_days + 1
    )
    new_download = _file(settings.downloads_dir / "vod2" / "b.mp4", age_days=0.1)
    old_tmp = _file(settings.tmp_dir / "job" / "chunk.wav", age_days=0.5)  # > 6 h
    fresh_tmp = _file(settings.tmp_dir / "job2" / "chunk.wav", age_days=0.01)
    sample = _file(settings.samples_dir / "demo.mp4", age_days=400)

    report = cleanup_storage(settings, db_session)

    assert not old_download.exists()
    assert not old_tmp.exists()
    assert new_download.exists() and fresh_tmp.exists()
    assert sample.exists(), "Beispieldateien dürfen nie automatisch gelöscht werden"
    assert report.deleted_files == 2
    assert report.freed_bytes == 2048


def test_dry_run_deletes_nothing(settings, db_session):
    ensure_directories(settings)
    old = _file(settings.renders_dir / "old.mp4", age_days=settings.storage_retention_days + 3)
    report = cleanup_storage(settings, db_session, dry_run=True)
    assert old.exists()
    assert report.deleted_files == 1 and report.dry_run


def test_videos_in_approval_pipeline_are_protected(settings, db_session):
    ensure_directories(settings)
    s = factories.streamer(db_session, "kanal")
    h_pending = factories.highlight(db_session, s, source_ref="a")
    h_done = factories.highlight(db_session, s, source_ref="b")
    pending_path = _file(settings.renders_dir / "pending.mp4", age_days=30)
    done_path = _file(settings.renders_dir / "done.mp4", age_days=30)
    v_pending = factories.rendered_video(db_session, h_pending, pending_path)
    v_done = factories.rendered_video(db_session, h_done, done_path)
    factories.publish_job(db_session, h_pending, v_pending, status="pending_review")
    factories.publish_job(db_session, h_done, v_done, status="published")
    db_session.commit()

    report = cleanup_storage(settings, db_session)
    db_session.commit()

    assert pending_path.exists()
    assert not done_path.exists()
    assert report.purged_videos == 1
    assert db_session.get(RenderedVideo, v_done.id).deleted_at is not None
    assert db_session.get(RenderedVideo, v_pending.id).deleted_at is None


def test_hard_size_limit_removes_oldest_first(settings, db_session, monkeypatch):
    monkeypatch.setattr(settings, "storage_max_gb", 3000 / 1024**3)  # ca. 3 KB
    ensure_directories(settings)
    oldest = _file(settings.downloads_dir / "1.mp4", size=1500, age_days=2)
    middle = _file(settings.downloads_dir / "2.mp4", size=1500, age_days=1)
    newest = _file(settings.renders_dir / "3.mp4", size=1500, age_days=0.5)
    report = cleanup_storage(settings, db_session)
    assert not oldest.exists()
    assert middle.exists() and newest.exists()
    assert report.deleted_files == 1


def test_is_within_blocks_traversal(settings):
    assert is_within(settings.data_dir / "renders" / "a.mp4", settings.data_dir)
    assert not is_within(settings.data_dir / ".." / "etc" / "passwd", settings.data_dir)


def test_cleanup_job_logs_activity(settings, database, db_session):
    from app.models import ActivityLog
    from app.workers.jobs.maintenance import cleanup_storage_job

    ensure_directories(settings)
    _file(settings.exports_dir / "x.mp4", age_days=settings.storage_retention_days + 1)
    result = cleanup_storage_job()
    assert result["deleted_files"] == 1
    assert db_session.query(ActivityLog).filter(ActivityLog.category == "system").count() == 1


def test_prune_activity(settings, database, db_session):
    from app.models import ActivityLog
    from app.workers.jobs.maintenance import prune_activity_job

    db_session.add(
        ActivityLog(
            level="info",
            category="system",
            message="alt",
            created_at=utcnow() - timedelta(days=settings.activity_retention_days + 1),
        )
    )
    db_session.add(ActivityLog(level="info", category="system", message="neu"))
    db_session.commit()
    assert prune_activity_job() == 1
    assert [e.message for e in db_session.query(ActivityLog).all()] == ["neu"]
