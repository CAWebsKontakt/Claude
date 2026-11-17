import time

import httpx
import respx
from rq.registry import ScheduledJobRegistry

from app.models import ActivityLog, Streamer, StreamSession
from app.workers import scheduler
from app.workers.jobs import twitch as jobs
from app.workers.queues import QUEUE_DEFAULT, get_queue
from tests import factories
from tests.conftest import post_form
from tests.twitch_payloads import API, AUTH, clip, stream, token_response, user


def _token():
    respx.post(f"{AUTH}/token").mock(return_value=httpx.Response(200, json=token_response()))


def test_jobs_skip_without_configuration(settings, database, redis):
    assert jobs.poll_live_status_job() == {"skipped": True}
    assert jobs.discover_clips_job() == {"skipped": True}
    assert jobs.schedule_streamer_refresh(1) is False


def test_jobs_skip_in_test_mode(twitch_settings, database, redis, monkeypatch):
    monkeypatch.setattr(twitch_settings, "test_mode", True)
    assert jobs.sync_eventsub_job() == {"skipped": True}


def test_scheduler_includes_twitch_jobs_only_when_configured(settings, twitch_settings):
    from app.config import Settings

    plain = Settings(_env_file=None, app_env="test", twitch_client_id="", twitch_client_secret="")
    assert {j.name for j in scheduler.build_periodic_jobs(plain)} == {"storage_cleanup", "prune_activity"}
    names = {j.name for j in scheduler.build_periodic_jobs(twitch_settings)}
    assert {"twitch_poll_live", "twitch_eventsub_sync", "twitch_discover_clips", "twitch_resolve"} <= names
    poll = next(j for j in scheduler.build_periodic_jobs(twitch_settings) if j.name == "twitch_poll_live")
    assert poll.interval_seconds == twitch_settings.twitch_poll_interval_seconds
    for job in scheduler.build_periodic_jobs(twitch_settings):
        module, func = job.func.rsplit(".", 1)
        assert callable(getattr(__import__(module, fromlist=[func]), func))


@respx.mock
def test_poll_job_schedules_post_stream_work(twitch_settings, database, db_session, redis):
    _token()
    s = factories.streamer(db_session, "kanal", twitch_user_id="111")
    db_session.commit()
    respx.get(f"{API}/streams").mock(
        return_value=httpx.Response(200, json={"data": [stream("111", "kanal", "5001")]})
    )
    assert jobs.poll_live_status_job()["live"] == 1
    respx.get(f"{API}/streams").mock(return_value=httpx.Response(200, json={"data": []}))
    jobs.poll_live_status_job()
    session = db_session.query(StreamSession).one()
    scheduled = set(ScheduledJobRegistry(queue=get_queue(QUEUE_DEFAULT, redis)).get_job_ids())
    assert {f"twitch-vod-{session.id}", f"twitch-clips-after-stream-{s.id}"} <= scheduled
    assert redis.hget("tcai:twitch:status", "last_poll_at")


@respx.mock
def test_errors_are_logged_throttled(twitch_settings, database, db_session, redis):
    _token()
    factories.streamer(db_session, "kanal", twitch_user_id="111")
    db_session.commit()
    respx.get(f"{API}/streams").mock(return_value=httpx.Response(500))
    jobs.HelixClient.__init__.__defaults__  # noqa: B018 – Klasse importierbar
    original_sleep = time.sleep
    try:
        time.sleep = lambda _s: None  # Backoff im Test nicht abwarten
        assert "error" in jobs.poll_live_status_job()
        assert "error" in jobs.poll_live_status_job()
    finally:
        time.sleep = original_sleep
    entries = db_session.query(ActivityLog).filter(ActivityLog.level == "error").all()
    assert len(entries) == 1 and "Twitch-Fehler" in entries[0].message


@respx.mock
def test_discover_job_stops_on_rate_limit(twitch_settings, database, db_session, redis):
    _token()
    factories.streamer(db_session, "kanal_a", twitch_user_id="111")
    factories.streamer(db_session, "kanal_b", twitch_user_id="222")
    db_session.commit()
    route = respx.get(f"{API}/clips").mock(
        return_value=httpx.Response(
            429, headers={"Ratelimit-Remaining": "0", "Ratelimit-Reset": str(int(time.time()) + 120)}
        )
    )
    assert jobs.discover_clips_job() == {}
    assert route.call_count == 1  # nach dem Rate-Limit keine weiteren Streamer


@respx.mock
def test_refresh_streamer_job(twitch_settings, database, db_session, redis):
    _token()
    s = factories.streamer(db_session, "kanal")
    db_session.commit()
    respx.get(f"{API}/users").mock(return_value=httpx.Response(200, json={"data": [user("111", "kanal")]}))
    respx.get(f"{API}/channels").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{API}/streams").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{API}/clips").mock(return_value=httpx.Response(200, json={"data": [clip("C1", "111")]}))
    result = jobs.refresh_streamer_job(s.id)
    assert result["clips"]["new"] == 1
    db_session.expire_all()
    assert db_session.get(Streamer, s.id).twitch_user_id == "111"
    assert get_queue(QUEUE_DEFAULT, redis).fetch_job("twitch-eventsub_sync") is not None


def test_adding_streamer_triggers_twitch_refresh(admin_client, twitch_settings, redis):
    response = post_form(admin_client, "/streamers", {"login": "neuerkanal"}, csrf_page="/streamers")
    assert response.status_code == 303
    page = admin_client.get(response.headers["location"]).text
    assert "im Hintergrund abgefragt" in page
    assert "Twitch jetzt abfragen" in page
    assert get_queue(QUEUE_DEFAULT, redis).count == 1


def test_system_and_clip_pages_show_twitch_features(admin_client, twitch_settings, db_session):
    s = factories.streamer(db_session, "kanal", twitch_user_id="111")
    factories.twitch_clip(db_session, s, score_breakdown={"tempo": 0.9, "aufrufe_pro_stunde": 120.0})
    db_session.commit()
    system = admin_client.get("/system").text
    assert "EventSub-Webhooks + Polling" in system
    assert "https://clips.example.com/webhooks/twitch/eventsub" in system
    clips = admin_client.get("/clips").text
    assert "clips.twitch.tv/embed?clip=AwkwardHelplessSalamanderSwiftRage&amp;parent=testserver" in clips
    assert "Warum diese Bewertung?" in clips and "Aufrufe pro Stunde" in clips
