"""Warteschlange, Freigabe nach TikTok-UX-Vorgaben, Upload, Statusverfolgung, Export (Phase 6)."""

from __future__ import annotations

import json
import zipfile
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from app.integrations.tiktok.client import CreatorInfo
from app.models import Highlight, PublishJob, StreamerPermission, User
from app.services import publishing
from app.services.publishing import ApprovalForm, PublishError
from app.timeutil import utcnow
from app.workers.jobs import publish as publish_jobs
from app.workers.queues import QUEUE_PUBLISH, get_queue
from tests import factories
from tests.conftest import post_form
from tests.tiktok_helpers import API, CREATOR_INFO, UPLOAD_URL, account, configure, envelope, rendered_setup

INFO = CreatorInfo.from_api(CREATOR_INFO)


def form(**overrides) -> ApprovalForm:
    values = {
        "account_id": None,
        "method": "manual_export",
        "caption": "Was für ein Moment! #twitch",
        "privacy_level": None,
        "allow_comment": False,
        "allow_duet": False,
        "allow_stitch": False,
        "commercial": False,
        "your_brand": False,
        "branded_content": False,
        "is_aigc": False,
        "consent": True,
        "rights_confirmed": True,
    }
    values.update(overrides)
    return ApprovalForm(**values)


@pytest.fixture
def editor(db_session, make_user) -> User:
    return db_session.scalar(select(User).where(User.username == "admin")) or make_user("admin", "admin")


@pytest.fixture
def setup(settings, monkeypatch, db_session, editor):
    configure(settings, monkeypatch)
    streamer, highlight, video = rendered_setup(db_session, settings)
    acc = account(db_session)
    job = publishing.add_to_queue(db_session, settings, highlight, actor=editor)
    db_session.commit()
    return {"streamer": streamer, "highlight": highlight, "video": video, "account": acc, "job": job}


# --- Warteschlange -------------------------------------------------------------------
def test_add_to_queue_requires_publishing_rights(settings, db_session, editor):
    _s, highlight, _v = rendered_setup(db_session, settings, publishing=False)
    with pytest.raises(PublishError, match="Veröffentlichung nicht erlaubt"):
        publishing.add_to_queue(db_session, settings, highlight, actor=editor)


def test_add_to_queue_creates_review_entry_once(setup, settings, db_session, editor):
    job, highlight = setup["job"], setup["highlight"]
    assert job.status == "pending_review" and highlight.status == "queued"
    assert job.caption.startswith("Beschreibung") and "#twitch" in job.caption
    assert job.video_cover_timestamp_ms == 4500  # Titelbild kurz nach dem lautesten Moment
    with pytest.raises(PublishError, match="bereits"):
        publishing.add_to_queue(db_session, settings, highlight, actor=editor)


def test_only_rendered_highlights_can_be_queued(settings, db_session, editor):
    s = factories.authorized_streamer(db_session, settings)
    h = factories.highlight(db_session, s, status="candidate")
    with pytest.raises(PublishError, match="geschnittenes Video"):
        publishing.add_to_queue(db_session, settings, h, actor=editor)


# --- Freigabe: Pflichtangaben und UX-Regeln ------------------------------------------
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"rights_confirmed": False}, "Erlaubnis"),
        ({"consent": False}, "Einwilligung"),
        ({"caption": "   "}, "leer"),
        ({"caption": "x" * 2201}, "2200"),
        ({"method": "direct_post", "privacy_level": None}, "Sichtbarkeit auswählen"),
        ({"method": "direct_post", "privacy_level": "FOLLOWER_OF_CREATOR"}, "nicht an"),
        ({"method": "direct_post", "privacy_level": "PUBLIC_TO_EVERYONE", "commercial": True}, "Marke"),
        (
            {
                "method": "direct_post",
                "privacy_level": "SELF_ONLY",
                "commercial": True,
                "branded_content": True,
            },
            "nicht privat",
        ),
    ],
)
def test_approval_validation(setup, settings, db_session, editor, overrides, message):
    data = {"account_id": setup["account"].id, **overrides}
    with pytest.raises(PublishError, match=message):
        publishing.approve(db_session, settings, setup["job"], form(**data), INFO, actor=editor)
    assert setup["job"].status == "pending_review"


def test_direct_post_needs_enabled_flag_and_scope(setup, settings, monkeypatch, db_session, editor):
    acc = setup["account"]
    assert publishing.available_methods(settings, acc) == ["direct_post", "inbox_upload", "manual_export"]
    monkeypatch.setattr(settings, "tiktok_direct_post_enabled", False)
    assert publishing.available_methods(settings, acc) == ["inbox_upload", "manual_export"]
    with pytest.raises(PublishError, match="nicht verfügbar"):
        publishing.approve(
            db_session,
            settings,
            setup["job"],
            form(account_id=acc.id, method="direct_post", privacy_level="SELF_ONLY"),
            INFO,
            actor=editor,
        )
    monkeypatch.setattr(settings, "test_mode", True)
    assert publishing.available_methods(settings, acc) == ["manual_export"]  # Testmodus lädt nie hoch
    assert publishing.available_methods(settings, None) == ["manual_export"]


def test_video_longer_than_creator_limit_is_rejected(setup, settings, db_session, editor):
    setup["video"].duration = 301
    with pytest.raises(PublishError, match="länger"):
        publishing.approve(
            db_session,
            settings,
            setup["job"],
            form(account_id=setup["account"].id, method="inbox_upload"),
            INFO,
            actor=editor,
        )


def test_direct_post_approval_stores_user_choices(setup, settings, db_session, editor):
    job = publishing.approve(
        db_session,
        settings,
        setup["job"],
        form(
            account_id=setup["account"].id,
            method="direct_post",
            privacy_level="PUBLIC_TO_EVERYONE",
            allow_comment=True,
            allow_duet=True,  # vom Creator deaktiviert → bleibt aus
            commercial=True,
            your_brand=True,
        ),
        INFO,
        actor=editor,
    )
    assert job.status == "approved" and job.approved_by_user_id == editor.id and job.consent_confirmed
    assert (job.disable_comment, job.disable_duet, job.disable_stitch) == (False, True, True)
    assert (job.brand_organic_toggle, job.brand_content_toggle) == (True, False)
    info = publishing.post_info(job)
    assert info["privacy_level"] == "PUBLIC_TO_EVERYONE" and info["video_cover_timestamp_ms"] == 4500


def test_approval_rechecks_rights(setup, settings, db_session, editor):
    permission = db_session.scalar(select(StreamerPermission))
    permission.revoked_at = utcnow()
    db_session.flush()
    db_session.expire(setup["streamer"])
    with pytest.raises(PublishError, match="nicht erlaubt"):
        publishing.approve(db_session, settings, setup["job"], form(), None, actor=editor)


# --- Manueller Export --------------------------------------------------------------------
def test_manual_export_zip(setup, settings, db_session, editor):
    job = publishing.approve(db_session, settings, setup["job"], form(), None, actor=editor)
    path = publishing.create_export(db_session, settings, job)
    with zipfile.ZipFile(path) as archive:
        assert sorted(archive.namelist()) == [
            "HINWEISE.txt",
            "beschreibung.txt",
            "untertitel.srt",
            "video.mp4",
        ]
        assert archive.read("beschreibung.txt").decode() == "Was für ein Moment! #twitch"
        assert "Keine urheberrechtlich geschützte Musik" in archive.read("HINWEISE.txt").decode()
    assert job.status == "exported" and job.tiktok_account_id is None
    assert db_session.get(Highlight, job.highlight_id).status == "published"


# --- Upload und Status (Direct Post / Inbox) ---------------------------------------------
def _approve(setup, settings, db_session, editor, **overrides):
    values = {"account_id": setup["account"].id, "method": "direct_post", "privacy_level": "SELF_ONLY"}
    values.update(overrides)
    job = publishing.approve(db_session, settings, setup["job"], form(**values), INFO, actor=editor)
    db_session.commit()
    return job.id


def _mock_publish(mock, *, init_path="/v2/post/publish/video/init/", statuses=()):
    mock.post(f"{API}/v2/post/publish/creator_info/query/").mock(return_value=envelope(CREATOR_INFO))
    init = mock.post(f"{API}{init_path}").mock(
        return_value=envelope({"publish_id": "v_pub_file~v2-1.123", "upload_url": UPLOAD_URL})
    )
    upload = mock.put(UPLOAD_URL).mock(return_value=httpx.Response(201))
    status = mock.post(f"{API}/v2/post/publish/status/fetch/").mock(
        side_effect=[envelope(s) for s in statuses] or [envelope({"status": "PROCESSING_UPLOAD"})]
    )
    return init, upload, status


def test_direct_post_end_to_end(setup, settings, database, db_session, editor, redis):
    job_id = _approve(setup, settings, db_session, editor, allow_comment=True)
    with respx.mock(assert_all_called=True) as mock:
        init, upload, status = _mock_publish(
            mock,
            statuses=[
                {"status": "PROCESSING_DOWNLOAD", "uploaded_bytes": 2066},
                {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7300000000000000001]},
            ],
        )
        result = publish_jobs.publish_job(job_id)
        assert result["publish_id"] == "v_pub_file~v2-1.123"
        body = json.loads(init.calls[0].request.content)
        assert body["post_info"]["privacy_level"] == "SELF_ONLY"
        assert body["post_info"]["disable_comment"] is False and body["post_info"]["disable_duet"] is True
        assert (
            body["source_info"]["source"] == "FILE_UPLOAD" and body["source_info"]["total_chunk_count"] == 1
        )
        assert upload.calls[0].request.headers["Content-Range"].startswith("bytes 0-")
        assert (
            f"tiktok-status-{job_id}-1"
            in get_queue(QUEUE_PUBLISH, redis).scheduled_job_registry.get_job_ids()
        )

        assert "pending" in publish_jobs.check_status_job(job_id)
        db_session.expire_all()
        job = db_session.get(PublishJob, job_id)
        assert job.status == "processing"
        assert publish_jobs.check_status_job(job_id) == {"skipped": "gerade geprüft"}  # nur eine Kette
        job.last_checked_at = utcnow() - timedelta(seconds=20)
        db_session.commit()
        assert publish_jobs.check_status_job(job_id)["final"] == "published"
    db_session.expire_all()
    job = db_session.get(PublishJob, job_id)
    assert job.status == "published" and job.tiktok_post_id == "7300000000000000001"
    assert job.published_at is not None and job.highlight.status == "published"
    assert job.uploaded_bytes == 2066


def test_inbox_upload_ends_in_inbox(setup, settings, database, db_session, editor, redis):
    job_id = _approve(setup, settings, db_session, editor, method="inbox_upload", privacy_level=None)
    with respx.mock(assert_all_called=True) as mock:
        init, _upload, _status = _mock_publish(
            mock, init_path="/v2/post/publish/inbox/video/init/", statuses=[{"status": "SEND_TO_USER_INBOX"}]
        )
        publish_jobs.publish_job(job_id)
        assert "post_info" not in json.loads(init.calls[0].request.content)
        assert publish_jobs.check_status_job(job_id)["final"] == "sent_to_inbox"
    db_session.expire_all()
    assert db_session.get(PublishJob, job_id).status == "sent_to_inbox"


def test_tiktok_failure_is_explained_and_retry_returns_to_review(
    setup, settings, database, db_session, editor, redis
):
    job_id = _approve(setup, settings, db_session, editor)
    with respx.mock as mock:
        _mock_publish(mock, statuses=[{"status": "FAILED", "fail_reason": "spam_risk_too_many_posts"}])
        publish_jobs.publish_job(job_id)
        publish_jobs.check_status_job(job_id)
    db_session.expire_all()
    job = db_session.get(PublishJob, job_id)
    assert job.status == "failed" and "Tageslimit" in job.fail_reason
    assert job.highlight.status == "rendered"
    publishing.reset_for_retry(db_session, job, actor=editor)
    assert job.status == "pending_review" and job.publish_id is None and not job.consent_confirmed


def test_rights_revoked_after_approval_blocks_upload(setup, settings, database, db_session, editor, redis):
    job_id = _approve(setup, settings, db_session, editor)
    db_session.scalar(select(StreamerPermission)).revoked_at = utcnow()
    db_session.commit()
    with respx.mock(assert_all_called=False) as mock:
        init, upload, _ = _mock_publish(mock)
        assert publish_jobs.publish_job(job_id) == {"error": "rights"}
        assert not init.called and not upload.called
    db_session.expire_all()
    assert "Rechteprüfung" in db_session.get(PublishJob, job_id).fail_reason


def test_publish_never_initializes_twice(setup, settings, database, db_session, editor, redis):
    job_id = _approve(setup, settings, db_session, editor)
    with respx.mock as mock:
        init, _upload, _ = _mock_publish(mock)
        publish_jobs.publish_job(job_id)
        assert publish_jobs.publish_job(job_id) == {"skipped": "bereits in Bearbeitung"}
    assert init.call_count == 1


def test_unapproved_job_is_never_uploaded(setup, database, redis):
    with respx.mock(assert_all_called=False) as mock:
        init, _u, _s = _mock_publish(mock)
        assert publish_jobs.publish_job(setup["job"].id) == {"skipped": "nicht freigegeben"}
        assert not init.called


def test_rate_limit_on_init_reschedules(setup, settings, database, db_session, editor, redis):
    job_id = _approve(setup, settings, db_session, editor)
    with respx.mock as mock:
        mock.post(f"{API}/v2/post/publish/creator_info/query/").mock(return_value=envelope(CREATOR_INFO))
        mock.post(f"{API}/v2/post/publish/video/init/").mock(
            return_value=httpx.Response(
                429, headers={"retry-after": "30"}, json={"error": {"code": "rate_limit_exceeded"}}
            )
        )
        assert publish_jobs.publish_job(job_id) == {"retry": "rate_limited"}
    db_session.expire_all()
    job = db_session.get(PublishJob, job_id)
    assert job.status == "approved" and job.publish_id is None and "Ratenlimit" in job.fail_reason
    assert any(
        j.startswith(f"publish-{job_id}-retry")
        for j in get_queue(QUEUE_PUBLISH, redis).scheduled_job_registry.get_job_ids()
    )


def test_privacy_option_removed_before_upload_fails_safely(
    setup, settings, database, db_session, editor, redis
):
    job_id = _approve(setup, settings, db_session, editor, privacy_level="PUBLIC_TO_EVERYONE")
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{API}/v2/post/publish/creator_info/query/").mock(
            return_value=envelope(dict(CREATOR_INFO, privacy_level_options=["SELF_ONLY"]))
        )
        init = mock.post(f"{API}/v2/post/publish/video/init/")
        publish_jobs.publish_job(job_id)
        assert not init.called
    db_session.expire_all()
    assert db_session.get(PublishJob, job_id).status == "failed"


def test_upload_failure_marks_failed(setup, settings, database, db_session, editor, redis):
    job_id = _approve(setup, settings, db_session, editor)
    with respx.mock as mock:
        _init, upload, _ = _mock_publish(mock)
        upload.mock(return_value=httpx.Response(500))
        assert publish_jobs.publish_job(job_id) == {"error": "upload"}
    db_session.expire_all()
    assert db_session.get(PublishJob, job_id).status == "failed"


def test_sync_resumes_lost_jobs(setup, settings, database, db_session, editor, redis):
    job_id = _approve(setup, settings, db_session, editor)
    other = factories.publish_job(db_session, setup["highlight"], setup["video"], status="uploading")
    other.updated_at = utcnow() - timedelta(hours=1)
    db_session.commit()
    result = publish_jobs.sync_publish_jobs_job()
    assert result == {"publish": 1, "status": 0, "released": 1}
    assert f"publish-{job_id}" in get_queue(QUEUE_PUBLISH, redis).job_ids
    db_session.expire_all()
    assert db_session.get(PublishJob, other.id).status == "approved"


def test_status_backoff_grows():
    job = PublishJob(approved_at=utcnow() - timedelta(seconds=5))
    assert publish_jobs.next_status_delay(job) == 15
    job.approved_at = utcnow() - timedelta(hours=3)
    assert publish_jobs.next_status_delay(job) == 3600


def test_scheduler_includes_tiktok_jobs(settings, monkeypatch):
    from app.workers.scheduler import build_periodic_jobs

    names = {j.name for j in build_periodic_jobs(settings)}
    assert "tiktok_sync_publish" in names and "tiktok_refresh_tokens" not in names
    configure(settings, monkeypatch)
    names = {j.name for j in build_periodic_jobs(settings)}
    assert {"tiktok_sync_publish", "tiktok_refresh_tokens"} <= names


# --- Dashboard ------------------------------------------------------------------------
def test_review_page_shows_live_creator_info_without_preselection(admin_client, setup, db_session):
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{API}/v2/post/publish/creator_info/query/").mock(
            return_value=envelope(CREATOR_INFO)
        )
        page = admin_client.get(f"/queue/{setup['job'].id}").text
    assert route.called  # live abgefragt
    assert '<option value="" selected>– bitte wählen –</option>' in page
    assert 'value="PUBLIC_TO_EVERYONE" selected' not in page
    assert "Music Usage Confirmation" in page and "Branded Content Policy" in page
    assert 'name="allow_duet" value="1" disabled' in page  # vom Creator deaktiviert
    assert 'name="allow_comment" value="1" checked' not in page  # Interaktionen nie vorausgewählt
    assert "@partner_clips" in page


def test_queue_highlight_and_export_via_dashboard(
    admin_client, settings, monkeypatch, db_session, editor, redis
):
    configure(settings, monkeypatch)
    _s, highlight, _v = rendered_setup(db_session, settings)
    db_session.commit()
    assert "Für TikTok vorbereiten" in admin_client.get(f"/highlights/{highlight.id}").text
    response = post_form(admin_client, f"/highlights/{highlight.id}/queue")
    assert response.status_code == 303
    job = db_session.scalar(select(PublishJob))
    assert response.headers["location"] == f"/queue/{job.id}"

    page = admin_client.get(f"/queue/{job.id}").text
    assert (
        "Manueller Export" in page
        and "Direct Post" not in page.split("Veröffentlichungsweg")[1].split("</fieldset>")[0]
    )
    r = post_form(
        admin_client,
        f"/queue/{job.id}/approve",
        {"method": "manual_export", "caption": "Text"},
        csrf_page=f"/queue/{job.id}",
    )
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(PublishJob, job.id).status == "pending_review"  # ohne Bestätigungen keine Freigabe
    r = post_form(
        admin_client,
        f"/queue/{job.id}/approve",
        {"method": "manual_export", "caption": "Text", "consent": "1", "rights_confirmed": "1"},
        csrf_page=f"/queue/{job.id}",
    )
    db_session.expire_all()
    job = db_session.get(PublishJob, job.id)
    assert job.status == "exported"
    download = admin_client.get(f"/media/exports/{job.id}.zip")
    assert download.status_code == 200 and download.headers["content-type"] == "application/zip"
    assert "ZIP" in admin_client.get("/published").text


def test_approve_direct_post_via_dashboard_enqueues_upload(admin_client, setup, db_session, redis):
    job = setup["job"]
    data = {
        "account_id": str(setup["account"].id),
        "method": "direct_post",
        "caption": "Clip #twitch",
        "privacy_level": "MUTUAL_FOLLOW_FRIENDS",
        "allow_comment": "1",
        "consent": "1",
        "rights_confirmed": "1",
    }
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{API}/v2/post/publish/creator_info/query/").mock(return_value=envelope(CREATOR_INFO))
        r = post_form(admin_client, f"/queue/{job.id}/approve", data, csrf_page="/queue")
    assert r.status_code == 303
    db_session.expire_all()
    job = db_session.get(PublishJob, job.id)
    assert job.status == "approved" and job.privacy_level == "MUTUAL_FOLLOW_FRIENDS"
    assert f"publish-{job.id}" in get_queue(QUEUE_PUBLISH, redis).job_ids


def test_viewer_cannot_queue_or_approve(client, make_user, setup, db_session):
    from tests.conftest import login

    make_user("leser", "viewer")
    login(client, "leser")
    assert post_form(client, f"/highlights/{setup['highlight'].id}/queue").status_code == 403
    assert (
        post_form(client, f"/queue/{setup['job'].id}/approve", {"method": "manual_export"}).status_code == 403
    )
    page = client.get(f"/queue/{setup['job'].id}").text
    assert "Prüfen &amp; freigeben" not in page


def test_export_download_requires_login(client, setup, settings, db_session, editor):
    job = publishing.approve(db_session, settings, setup["job"], form(), None, actor=editor)
    publishing.create_export(db_session, settings, job)
    db_session.commit()
    assert Path(job.export_path).is_file()
    r = client.get(f"/media/exports/{job.id}.zip", follow_redirects=False)
    assert r.status_code in (303, 401)


def test_status_check_reschedules_itself_inside_rq_worker(
    setup, settings, database, db_session, editor, redis
):
    """Regression: Die Folgeabfrage darf nicht dieselbe Job-ID wie die laufende Abfrage haben."""
    from rq import SimpleWorker

    job_id = _approve(setup, settings, db_session, editor)
    with respx.mock as mock:
        _mock_publish(mock, statuses=[{"status": "PROCESSING_UPLOAD"}] * 3)
        publish_jobs.publish_job(job_id)
        queue = get_queue(QUEUE_PUBLISH, redis)
        for registry_id in queue.scheduled_job_registry.get_job_ids():
            queue.scheduled_job_registry.remove(registry_id)
        publish_jobs.schedule_status_check(job_id, 0)  # wie Abgleich/Button: Basis-ID
        SimpleWorker([queue], connection=redis).work(burst=True)
        follow_ups = queue.scheduled_job_registry.get_job_ids()
    assert len(follow_ups) == 1 and follow_ups[0].startswith(f"tiktok-status-{job_id}-")
