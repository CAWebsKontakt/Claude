"""Produktionsbetrieb (Phase 7): Alarmierung, Metriken, Selbsttest, Härtung."""

from __future__ import annotations

import shutil
from datetime import timedelta

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.models import PublishJob, StreamerPermission
from app.services import alerts
from app.timeutil import utcnow
from tests import factories
from tests.tiktok_helpers import account, rendered_setup

HOOK = "https://ntfy.example/twitchclip-alarm"


@pytest.fixture
def healthy(monkeypatch):
    """System gilt als gesund (Worker + Scheduler aktiv, genug Speicher) – Fachprüfungen bleiben echt."""
    from app.services import system

    real = system.collect_status

    def fake(settings, db, redis, *, detailed=True):
        status = real(settings, db, redis, detailed=False)
        status.workers = [{"name": "w1", "queues": "default", "state": "idle", "last_heartbeat": None}]
        status.scheduler_age_seconds = 5
        status.disk = {"total_gb": 100.0, "used_gb": 40.0, "free_gb": 60.0, "percent_used": 40.0}
        status.data_size_gb = 1.0
        return status

    monkeypatch.setattr(alerts, "collect_status", fake)


# --- Alarmierung ----------------------------------------------------------------------
def test_no_alerts_when_healthy(settings, db_session, redis, healthy):
    assert alerts.collect_alerts(settings, db_session, redis) == []


def test_system_alerts_without_worker_and_scheduler(settings, db_session, redis, monkeypatch):
    monkeypatch.setattr(
        "app.services.system.disk_usage",
        lambda _p: {"total_gb": 100.0, "used_gb": 97.0, "free_gb": 3.0, "percent_used": 97.0},
    )
    keys = {a.key for a in alerts.collect_alerts(settings, db_session, redis)}
    assert {"workers", "scheduler", "disk"} <= keys


def test_domain_alerts(settings, db_session, redis, healthy, make_user):
    make_user()
    s = factories.authorized_streamer(db_session, settings, "ohne_token", twitch_user_id="333")
    for token in s.oauth_tokens:
        token.revoked_at = utcnow()
    expiring = factories.authorized_streamer(db_session, settings, "laeuft_ab", twitch_user_id="222")
    permission = db_session.query(StreamerPermission).filter_by(streamer_id=expiring.id).one()
    permission.valid_until = utcnow() + timedelta(days=3)
    acc = account(db_session)
    for token in acc.oauth_tokens:
        token.revoked_at = utcnow()
    _s, highlight, video = rendered_setup(db_session, settings)
    job = factories.publish_job(db_session, highlight, video, status="failed")
    job.fail_reason = "Tageslimit für Posts erreicht"
    old = factories.publish_job(db_session, highlight, video)
    old.created_at = utcnow() - timedelta(days=4)
    db_session.flush()
    found = {a.key: a.message for a in alerts.collect_alerts(settings, db_session, redis)}
    assert f"twitch-oauth-{s.id}" in found
    assert f"permission-expiry-{permission.id}" in found
    assert f"tiktok-account-{acc.id}" in found
    assert "Tageslimit" in found[f"publish-failed-{job.id}"]
    assert "publish-review-backlog" in found


def test_alerts_are_sent_once_and_resolved(settings, db_session, redis, healthy, monkeypatch):
    monkeypatch.setattr(settings, "alert_webhook_url", HOOK)
    problem = [alerts.Alert("x", "error", "Kein Worker aktiv")]
    monkeypatch.setattr(alerts, "collect_alerts", lambda *_a: list(problem))
    with respx.mock, httpx.Client() as http:
        route = respx.post(HOOK).mock(return_value=httpx.Response(200))
        first = alerts.run_alerts(settings, db_session, redis, http)
        second = alerts.run_alerts(settings, db_session, redis, http)
        assert (first["notified"], second["notified"]) == (1, 0)  # gedrosselt
        assert route.call_count == 1
        assert "Kein Worker aktiv" in route.calls[0].request.content.decode()
        assert route.calls[0].request.headers["content-type"].startswith("text/plain")
        problem.clear()
        third = alerts.run_alerts(settings, db_session, redis, http)
        assert third["resolved"] == 1 and route.call_count == 2
        assert "behoben" in route.calls[1].request.content.decode()
        problem.append(alerts.Alert("x", "error", "Kein Worker aktiv"))
        assert alerts.run_alerts(settings, db_session, redis, http)["notified"] == 1  # nach Entwarnung neu


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://discord.com/api/webhooks/1/abc", "content"),
        ("https://hooks.slack.com/services/T/B/x", "text"),
    ],
)
def test_alert_payload_formats(settings, monkeypatch, url, key):
    import json

    monkeypatch.setattr(settings, "alert_webhook_url", url)
    with respx.mock, httpx.Client() as http:
        route = respx.post(url).mock(return_value=httpx.Response(204))
        assert alerts.send(settings, http, "Test")
    assert json.loads(route.calls[0].request.content) == {key: "Test"}


def test_alert_delivery_failure_is_tolerated(settings, monkeypatch):
    monkeypatch.setattr(settings, "alert_webhook_url", HOOK)
    with respx.mock, httpx.Client() as http:
        respx.post(HOOK).mock(side_effect=httpx.ConnectError("down"))
        assert alerts.send(settings, http, "Test") is False


def test_alert_job_is_scheduled(settings, monkeypatch):
    from app.workers.scheduler import build_periodic_jobs

    jobs = {j.name: j for j in build_periodic_jobs(settings)}
    assert jobs["alerts"].interval_seconds == settings.alert_check_interval_minutes * 60


# --- Metriken --------------------------------------------------------------------------
def test_metrics_disabled_without_token(client):
    assert client.get("/metrics").status_code == 404


def test_metrics_require_bearer_token(client, settings, monkeypatch, db_session):
    monkeypatch.setattr(settings, "metrics_token", SecretStr("geheim-123"))
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer falsch"}).status_code == 401
    _s, highlight, video = rendered_setup(db_session, settings)
    factories.publish_job(db_session, highlight, video)
    db_session.commit()
    response = client.get("/metrics", headers={"Authorization": "Bearer geheim-123"})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "# TYPE tcai_queue_jobs gauge" in body
    assert 'tcai_publish_jobs{status="pending_review"} 1' in body
    assert 'tcai_streamers{mode="authorized"} 1' in body
    assert "tcai_database_up 1" in body and "tcai_redis_up 1" in body


# --- Selbsttest (echte FFmpeg-Kette) ------------------------------------------------------
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg nicht installiert")


def test_selftest_requires_test_mode(settings, database, make_user):
    from app.services.selftest import run_selftest

    make_user()
    result = run_selftest(settings, transcribe=False)
    assert not result.ok and "TEST_MODE" in result.steps[0].detail


@needs_ffmpeg
def test_selftest_runs_whole_pipeline(settings, database, db_session, redis, make_user, monkeypatch):
    from app.services.selftest import run_selftest

    make_user()
    monkeypatch.setattr(settings, "test_mode", True)
    monkeypatch.setattr(settings, "render_preset", "ultrafast")
    seen = []
    result = run_selftest(settings, transcribe=False, duration=30, report=seen.append)
    assert result.ok, [(s.name, s.detail) for s in result.steps]
    assert [s.name for s in seen][-1] == "Freigabe & Export"
    db_session.expire_all()
    job = db_session.get(PublishJob, result.job_id)
    assert job.status == "exported" and job.method == "manual_export"


@needs_ffmpeg
def test_tagged_samples_are_unique(settings):
    from app.services.media_probe import sha256_file
    from app.services.samples import generate_samples

    first = generate_samples(settings, count=1, duration=10, tag="selbsttest-a")
    second = generate_samples(settings, count=1, duration=10, tag="selbsttest-b")
    assert first[0].name == "selbsttest-a-01-10s.mp4"
    assert sha256_file(first[0]) != sha256_file(second[0])  # wiederholte Selbsttests sind keine Duplikate


def test_cli_selftest_exit_code(settings, database, make_user, capsys):
    from app.cli import main

    make_user()
    assert main(["selftest", "--no-transcribe"]) == 1  # Testmodus aus → Fehlschlag mit Erklärung
    assert "TEST_MODE" in capsys.readouterr().out
