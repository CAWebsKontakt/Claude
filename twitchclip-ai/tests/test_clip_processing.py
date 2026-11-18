import hashlib
import shutil
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from rq.registry import ScheduledJobRegistry

from app.integrations.twitch.client import HelixClient
from app.models import ActivityLog, ClipRequest, OAuthToken
from app.services import clip_processing as cp
from app.services.storage import cleanup_storage, ensure_directories
from app.services.streamers import RightsError
from app.timeutil import utcnow
from app.workers.jobs import clips as clip_jobs
from app.workers.queues import QUEUE_DEFAULT, QUEUE_MEDIA, get_queue
from tests import factories
from tests.conftest import post_form
from tests.twitch_payloads import API, AUTH, token_response

VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"x" * 50_000
CDN = "https://production.assets.clips.twitchcdn.net/v2/media/abc/video.mp4"


def _clip(db, streamer, clip_id, *, offset, score, duration=30.0, video="v-100"):
    return factories.twitch_clip(
        db,
        streamer,
        clip_id=clip_id,
        vod_offset=offset,
        video_id=video,
        discovery_score=score,
        duration=duration,
    )


@pytest.mark.parametrize(
    ("value", "seconds"), [("1:02:03", 3723), ("62:03", 3723), ("3723", 3723), ("0:05", 5)]
)
def test_parse_timecode(value, seconds):
    assert cp.parse_timecode(value) == seconds


@pytest.mark.parametrize("value", ["", "abc", "1:2:3:4", "-5"])
def test_parse_timecode_invalid(value):
    with pytest.raises(cp.ClipRequestError):
        cp.parse_timecode(value)


def test_plan_viewer_clips(db_session, twitch_settings, make_user, monkeypatch):
    make_user()
    monkeypatch.setattr(twitch_settings, "clip_candidates_per_stream", 3)
    s = factories.authorized_streamer(db_session, twitch_settings)
    session = factories.stream_session(db_session, s)
    _clip(db_session, s, "A", offset=1000, score=95)
    _clip(db_session, s, "A-same-moment", offset=1020, score=90)  # zu nah an A
    _clip(db_session, s, "B", offset=3000, score=80)
    _clip(db_session, s, "C", offset=5000, score=70)
    _clip(db_session, s, "D", offset=7000, score=60)  # über dem Limit von 3
    _clip(db_session, s, "anderes-vod", offset=100, score=99, video="v-999")
    db_session.commit()

    planned = cp.plan_viewer_clips(db_session, twitch_settings, session)
    assert [r.twitch_clip_id for r in planned] == ["A", "B", "C"]
    first = planned[0]
    assert first.vod_offset == 1030 and first.duration == 30  # vod_offset = Ende des Clips
    assert first.start_seconds == 1000 and first.status == "planned"
    assert cp.plan_viewer_clips(db_session, twitch_settings, session) == []  # keine Duplikate


def test_planning_requires_rights(db_session, twitch_settings, make_user):
    make_user()
    public = factories.streamer(db_session, "oeffentlich", twitch_user_id="7")
    session = factories.stream_session(db_session, public)
    with pytest.raises(RightsError):
        cp.plan_viewer_clips(db_session, twitch_settings, session)


def test_request_vod_clip_validation(db_session, twitch_settings, make_user):
    admin = make_user()
    s = factories.authorized_streamer(db_session, twitch_settings)
    session = factories.stream_session(db_session, s)
    request = cp.request_vod_clip(db_session, s, session, start_seconds=600, duration=30, actor=admin)
    assert (request.vod_offset, request.duration, request.source) == (630, 30, "manual")
    with pytest.raises(cp.ClipRequestError, match="bereits"):
        cp.request_vod_clip(db_session, s, session, start_seconds=600, duration=30, actor=admin)
    with pytest.raises(cp.ClipRequestError, match="zwischen 5 und 60"):
        cp.request_vod_clip(db_session, s, session, start_seconds=10, duration=90, actor=admin)
    no_vod = factories.stream_session(db_session, s, vod_id=None, stream_id="6000")
    with pytest.raises(cp.ClipRequestError, match="kein VOD"):
        cp.request_vod_clip(db_session, s, no_vod, start_seconds=10, duration=30, actor=admin)
    s.oauth_tokens[0].revoked_at = utcnow()
    db_session.flush()
    with pytest.raises(cp.ClipRequestError, match="Twitch-Autorisierung fehlt"):
        cp.request_vod_clip(db_session, s, session, start_seconds=900, duration=30, actor=admin)


@respx.mock
def test_create_clip_from_vod(db_session, twitch_settings, make_user, redis):
    admin = make_user()
    s = factories.authorized_streamer(db_session, twitch_settings)
    session = factories.stream_session(db_session, s)
    request = cp.request_vod_clip(
        db_session, s, session, start_seconds=600, duration=30, actor=admin, title="Clutch"
    )
    route = respx.post(f"{API}/videos/clips").mock(
        return_value=httpx.Response(
            202,
            json={"data": [{"id": "NeuerClip123", "edit_url": "https://clips.twitch.tv/NeuerClip123/edit"}]},
        )
    )
    with HelixClient(twitch_settings, redis, sleep=lambda _s: None) as client:
        assert cp.create_clip(db_session, twitch_settings, client, request)
    params = route.calls.last.request.url.params
    assert params["editor_id"] == "111" and params["broadcaster_id"] == "111" and params["vod_id"] == "v-100"
    assert params["vod_offset"] == "630" and params["duration"] == "30" and params["title"] == "Clutch"
    assert route.calls.last.request.headers["Authorization"] == "Bearer user-access-1"
    assert request.twitch_clip_id == "NeuerClip123" and request.status == "waiting"


def _mock_download(clip_id="A", body=VIDEO_BYTES, *, ready=True):
    data = (
        [{"clip_id": clip_id, "landscape_download_url": CDN, "portrait_download_url": None}] if ready else []
    )
    respx.get(f"{API}/clips/downloads").mock(return_value=httpx.Response(200, json={"data": data}))
    return respx.get(CDN).mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "video/mp4"})
    )


@respx.mock
def test_download_clip_and_duplicates(db_session, twitch_settings, make_user, redis):
    make_user()
    s = factories.authorized_streamer(db_session, twitch_settings)
    session = factories.stream_session(db_session, s)
    _clip(db_session, s, "A", offset=1000, score=95)
    _clip(db_session, s, "B", offset=3000, score=90)
    first, second = cp.plan_viewer_clips(db_session, twitch_settings, session)
    cdn = _mock_download()
    with HelixClient(twitch_settings, redis, sleep=lambda _s: None) as client:
        result = cp.download_clip(db_session, twitch_settings, client, first)
        assert "Authorization" not in cdn.calls.last.request.headers  # kein Token an das CDN
        assert first.status == "downloaded" and Path(first.file_path).read_bytes() == VIDEO_BYTES
        assert first.sha256 == hashlib.sha256(VIDEO_BYTES).hexdigest() == result.sha256
        assert Path(first.file_path).is_relative_to(twitch_settings.downloads_dir)
        # Zweiter Clip liefert identische Datei -> Duplikat, Datei wird nicht doppelt gespeichert
        respx.get(f"{API}/clips/downloads").mock(
            return_value=httpx.Response(200, json={"data": [{"clip_id": "B", "landscape_download_url": CDN}]})
        )
        dup = cp.download_clip(db_session, twitch_settings, client, second)
    assert dup.duplicate_of == first.id and second.status == "duplicate"
    assert len(list(twitch_settings.downloads_dir.rglob("*.mp4"))) == 1


@respx.mock
def test_download_not_ready_and_size_limit(db_session, twitch_settings, make_user, redis, monkeypatch):
    make_user()
    s = factories.authorized_streamer(db_session, twitch_settings)
    session = factories.stream_session(db_session, s)
    _clip(db_session, s, "A", offset=1000, score=95)
    (request,) = cp.plan_viewer_clips(db_session, twitch_settings, session)
    with HelixClient(twitch_settings, redis, sleep=lambda _s: None) as client:
        _mock_download(ready=False)
        with pytest.raises(cp.ClipNotReadyError):
            cp.download_clip(db_session, twitch_settings, client, request)
        monkeypatch.setattr(twitch_settings, "clip_download_max_mb", 0)
        _mock_download()
        with pytest.raises(ValueError, match="größer"):
            cp.download_clip(db_session, twitch_settings, client, request)
    assert not list(twitch_settings.tmp_dir.glob("*.part")), "keine Reste im tmp-Verzeichnis"


@respx.mock
def test_rights_rechecked_before_download(db_session, twitch_settings, make_user, redis):
    make_user()
    s = factories.authorized_streamer(db_session, twitch_settings)
    session = factories.stream_session(db_session, s)
    _clip(db_session, s, "A", offset=1000, score=95)
    (request,) = cp.plan_viewer_clips(db_session, twitch_settings, session)
    s.permissions[0].revoked_at = utcnow()  # Erlaubnis zwischenzeitlich widerrufen
    db_session.flush()
    cdn = _mock_download()
    with HelixClient(twitch_settings, redis, sleep=lambda _s: None) as client:
        assert cp.download_clip(db_session, twitch_settings, client, request) is None
    assert request.status == "skipped" and "Erlaubnis fehlt" in request.error
    assert cdn.call_count == 0


@respx.mock
def test_jobs_create_then_schedule_download(twitch_settings, database, db_session, redis, make_user):
    admin = make_user()
    respx.post(f"{AUTH}/token").mock(return_value=httpx.Response(200, json=token_response()))
    s = factories.authorized_streamer(db_session, twitch_settings)
    session = factories.stream_session(db_session, s)
    request = cp.request_vod_clip(db_session, s, session, start_seconds=60, duration=20, actor=admin)
    db_session.commit()
    respx.post(f"{API}/videos/clips").mock(return_value=httpx.Response(202, json={"data": [{"id": "X1"}]}))
    assert clip_jobs.create_clip_job(request.id) == {"created": True}
    scheduled = ScheduledJobRegistry(queue=get_queue(QUEUE_MEDIA, redis)).get_job_ids()
    assert f"clips-download-{request.id}-1" in scheduled

    # Download noch nicht bereit -> Wiederholung eingeplant, Status "Twitch verarbeitet"
    _mock_download("X1", ready=False)
    assert clip_jobs.download_clip_job(request.id, 1) == {"retry": 2}
    db_session.expire_all()
    assert db_session.get(ClipRequest, request.id).status == "waiting"
    # Letzter Versuch scheitert endgültig
    last = len(clip_jobs.DOWNLOAD_RETRY_DELAYS) + 1
    assert "error" in clip_jobs.download_clip_job(request.id, last)
    db_session.expire_all()
    failed = db_session.get(ClipRequest, request.id)
    assert failed.status == "failed"
    assert db_session.query(ActivityLog).filter(ActivityLog.level == "error").count() == 1


@respx.mock
def test_forbidden_marks_editor_token(twitch_settings, database, db_session, redis, make_user):
    admin = make_user()
    s = factories.authorized_streamer(db_session, twitch_settings, role="editor", token_user_id="999")
    session = factories.stream_session(db_session, s)
    request = cp.request_vod_clip(db_session, s, session, start_seconds=60, duration=20, actor=admin)
    db_session.commit()
    respx.post(f"{API}/videos/clips").mock(
        return_value=httpx.Response(403, json={"message": "user is not an editor"})
    )
    assert clip_jobs.create_clip_job(request.id) == {"error": "forbidden"}
    db_session.expire_all()
    assert db_session.get(ClipRequest, request.id).status == "failed"
    assert db_session.query(OAuthToken).one().revoked_at is not None


def test_dashboard_manual_clip_request(admin_client, db_session, twitch_settings, redis):
    s = factories.authorized_streamer(db_session, twitch_settings)
    session = factories.stream_session(db_session, s)
    db_session.commit()
    page = admin_client.get(f"/streamers/{s.id}").text
    assert "Streams &amp; Clip-Verarbeitung" in page and "Clip erstellen" in page
    post_form(
        admin_client, f"/streamers/{s.id}/sessions/{session.id}/clip", {"start": "1:00:00", "duration": "40"}
    )
    request = db_session.query(ClipRequest).one()
    assert (request.vod_offset, request.duration) == (3640, 40)
    assert get_queue(QUEUE_DEFAULT, redis).fetch_job(f"clips-create-{request.id}-1") is not None
    assert (
        "Wird erstellt" in admin_client.get(f"/streamers/{s.id}").text
        or "Geplant" in admin_client.get(f"/streamers/{s.id}").text
    )


def test_clip_media_and_cleanup(client, make_user, db_session, settings):
    from tests.conftest import login

    make_user()
    ensure_directories(settings)
    s = factories.streamer(db_session, "kanal")
    path = settings.downloads_dir / "1" / "clip-1.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(VIDEO_BYTES)
    request = ClipRequest(
        streamer_id=s.id,
        source="viewer_clip",
        dedup_key="viewer:X",
        status="downloaded",
        file_path=str(path),
        file_size=len(VIDEO_BYTES),
    )
    db_session.add(request)
    db_session.commit()
    login(client)
    assert client.get(f"/media/clips/{request.id}.mp4").status_code == 200

    import os
    import time

    old = time.time() - (settings.storage_retention_days + 1) * 86400
    os.utime(path, (old, old))
    cleanup_storage(settings, db_session)
    db_session.commit()
    db_session.refresh(request)
    assert request.file_path is None and "gelöscht" in request.error
    assert client.get(f"/media/clips/{request.id}.mp4").status_code == 404


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg nicht installiert")
def test_test_mode_samples(settings, db_session, monkeypatch):
    from app.services.samples import generate_samples
    from app.services.streamers import compute_rights

    monkeypatch.setattr(settings, "test_mode", True)
    created = generate_samples(settings, count=1, duration=10)
    assert created and created[0].exists()
    requests = cp.ingest_samples(db_session, settings)
    assert len(requests) == 1
    request = requests[0]
    assert request.status == "downloaded" and request.source == "sample"
    assert request.media_info["width"] == 1920 and request.media_info["has_audio"]
    demo = request.streamer
    assert demo.is_demo and compute_rights(demo).can_publish
    assert cp.ingest_samples(db_session, settings) == []  # keine Doppelübernahme
    monkeypatch.setattr(settings, "test_mode", False)
    assert not compute_rights(demo).can_process


def test_resume_only_old_stuck_requests(twitch_settings, database, db_session, redis, make_user):
    make_user()
    s = factories.authorized_streamer(db_session, twitch_settings)
    old = ClipRequest(
        streamer_id=s.id,
        source="viewer_clip",
        dedup_key="viewer:old",
        status="waiting",
        twitch_clip_id="OLD",
        attempts=2,
        updated_at=utcnow() - timedelta(hours=3),
    )
    fresh = ClipRequest(
        streamer_id=s.id,
        source="viewer_clip",
        dedup_key="viewer:fresh",
        status="waiting",
        twitch_clip_id="NEW",
        attempts=1,
    )
    db_session.add_all([old, fresh])
    db_session.commit()
    assert clip_jobs.resume_stuck_requests_job() == {"resumed": [old.id]}


@respx.mock
def test_cdn_connection_error_is_retried(twitch_settings, database, db_session, redis, make_user):
    make_user()
    s = factories.authorized_streamer(db_session, twitch_settings)
    session = factories.stream_session(db_session, s)
    _clip(db_session, s, "A", offset=1000, score=95)
    (request,) = cp.plan_viewer_clips(db_session, twitch_settings, session)
    db_session.commit()
    respx.get(f"{API}/clips/downloads").mock(
        return_value=httpx.Response(200, json={"data": [{"clip_id": "A", "landscape_download_url": CDN}]})
    )
    respx.get(CDN).mock(side_effect=httpx.ConnectError("Connection refused"))
    assert clip_jobs.download_clip_job(request.id, 1) == {"retry": 2}
    db_session.expire_all()
    row = db_session.get(ClipRequest, request.id)
    assert row.status == "waiting" and "nicht erreichbar" in row.error
    assert not list(twitch_settings.tmp_dir.glob("*.part"))


def test_retry_after_reauthorization(admin_client, db_session, twitch_settings, redis):
    s = factories.authorized_streamer(db_session, twitch_settings)
    session = factories.stream_session(db_session, s)
    _clip(db_session, s, "A", offset=1000, score=95)
    (request,) = cp.plan_viewer_clips(db_session, twitch_settings, session)
    request.status = "skipped"
    request.error = "Twitch-Autorisierung ungültig"
    s.oauth_tokens[0].revoked_at = utcnow()
    db_session.commit()
    page = admin_client.get(f"/streamers/{s.id}").text
    assert "Erneut versuchen" in page
    post_form(admin_client, f"/streamers/{s.id}/clip-requests/{request.id}/retry")
    db_session.expire_all()
    assert (
        db_session.get(ClipRequest, request.id).status == "skipped"
    )  # ohne gültige Autorisierung kein Neustart
    assert "Twitch-Autorisierung fehlt" in admin_client.get(f"/streamers/{s.id}").text

    token = db_session.query(OAuthToken).one()
    token.revoked_at = None
    db_session.commit()
    post_form(admin_client, f"/streamers/{s.id}/clip-requests/{request.id}/retry")
    db_session.expire_all()
    row = db_session.get(ClipRequest, request.id)
    assert row.status == "waiting" and row.attempts == 0 and row.error is None
    jobs = get_queue(QUEUE_MEDIA, redis).job_ids
    assert any(j.startswith(f"clips-download-{request.id}-r") and j.endswith("-1") for j in jobs)


def test_retry_skipped_analysis(admin_client, db_session, twitch_settings, redis, settings):
    s = factories.authorized_streamer(db_session, twitch_settings)
    path = twitch_settings.downloads_dir / "1" / "clip-9.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(VIDEO_BYTES)
    request = ClipRequest(
        streamer_id=s.id,
        source="viewer_clip",
        dedup_key="viewer:ana",
        status="downloaded",
        file_path=str(path),
        error="Analyse: Twitch-Autorisierung fehlt",
    )
    db_session.add(request)
    db_session.commit()
    assert "Erneut versuchen" in admin_client.get(f"/streamers/{s.id}").text
    post_form(admin_client, f"/streamers/{s.id}/clip-requests/{request.id}/retry")
    db_session.expire_all()
    assert db_session.get(ClipRequest, request.id).error is None
    assert get_queue(QUEUE_MEDIA, redis).fetch_job(f"analyze-{request.id}") is not None
