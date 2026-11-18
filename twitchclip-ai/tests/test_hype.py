"""Hype-Liste: wachsende Kanäle aus öffentlichen Daten finden (Modus A)."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx

from app.integrations.twitch.client import HelixClient
from app.models import Streamer
from app.services import hype
from app.timeutil import utcnow
from app.workers import scheduler
from app.workers.jobs import twitch as jobs
from tests import factories
from tests.conftest import post_form
from tests.twitch_payloads import API, AUTH, clip, stream, token_response


def _token():
    respx.post(f"{AUTH}/token").mock(return_value=httpx.Response(200, json=token_response()))


def _streams(*items):
    return httpx.Response(200, json={"data": list(items), "pagination": {}})


def _clips(*items):
    return httpx.Response(200, json={"data": list(items), "pagination": {}})


# --- Bewertung --------------------------------------------------------------------
def test_components_and_weight_redistribution():
    assert hype.growth_component(None) is None and hype.growth_component(-20) == 0
    assert hype.growth_component(50) == 0.5 and hype.growth_component(300) == 1.0
    assert hype.clip_component(0) == 0 and hype.clip_component(10) == 1.0
    assert 0.25 < hype.clip_component(1) < 0.35
    assert hype.size_component(1000) == 1.0 and hype.size_component(100) == 0.5
    assert hype.size_component(25000) == 0.2

    fresh = hype.HypeEntry("1", "a", "A", "", "", viewers=1000, started_at=None)
    hype.score(fresh)  # kein Verlauf, keine Clips → nur Größe, Wachstum umverteilt, Clips = 0
    assert fresh.score == 27.3 and set(fresh.components) == {"clips", "groesse"}
    clipping = hype.HypeEntry("4", "d", "D", "", "", viewers=1000, started_at=None, clips_3h=10)
    assert hype.score(clipping) == 100.0  # erste Stunde: Clips + Größe entscheiden
    rising = hype.HypeEntry("2", "b", "B", "", "", viewers=1000, started_at=None, growth_pct=100, clips_3h=10)
    flat = hype.HypeEntry("3", "c", "C", "", "", viewers=1000, started_at=None, growth_pct=0, clips_3h=0)
    assert hype.score(rising) == 100.0 and hype.score(flat) == 15.0


def test_baseline_prefers_sample_about_one_hour_ago(redis):
    now = utcnow()
    for minutes, viewers in [(120, 100), (65, 300), (30, 500), (5, 900)]:
        hype.record_sample(redis, "42", viewers, now - timedelta(minutes=minutes))
    assert hype.baseline(redis, "42", now) == (300, 65)
    hype.record_sample(redis, "43", 100, now - timedelta(minutes=10))
    assert hype.baseline(redis, "43", now) is None  # jünger als 20 min zählt nicht


# --- Lauf gegen die (gemockte) Twitch-API ---------------------------------------------
@respx.mock
def test_scan_ranks_growing_channels_with_clip_activity(db_session, twitch_settings, redis, monkeypatch):
    monkeypatch.setattr(twitch_settings, "hype_clip_checks", 5)
    _token()
    now = utcnow()
    # Vor einer Stunde: „aufsteiger“ hatte 300, „stabil“ 2000 Zuschauer
    hype.record_sample(redis, "1", 300, now - timedelta(minutes=60))
    hype.record_sample(redis, "2", 2000, now - timedelta(minutes=60))
    route = respx.get(f"{API}/streams").mock(
        return_value=_streams(
            stream("2", "stabil", "s2", viewers=2000, game="Just Chatting"),
            stream("1", "aufsteiger", "s1", viewers=900, game="Valorant"),
            stream("3", "zu_klein", "s3", viewers=10),
            stream("4", "riesig", "s4", viewers=90000),
        )
    )
    respx.get(f"{API}/clips", params={"broadcaster_id": "1"}).mock(
        return_value=_clips(
            *[clip(f"c{i}", "1", views=50 * i, created=now - timedelta(minutes=20 + i)) for i in range(6)]
        )
    )
    respx.get(f"{API}/clips", params={"broadcaster_id": "2"}).mock(
        return_value=_clips(clip("old", "2", views=10, created=now - timedelta(hours=10)))
    )
    known = factories.streamer(db_session, "aufsteiger", twitch_user_id="1")
    db_session.commit()

    with HelixClient(twitch_settings, redis) as client:
        result = hype.scan(db_session, twitch_settings, redis, client, now=now)

    query = dict(route.calls[0].request.url.params)
    assert query["language"] == "de" and query["type"] == "live"
    assert result.scanned == 4 and result.in_range == 2  # zu klein / zu groß gefiltert
    first, second = result.entries
    assert first.login == "aufsteiger" and first.growth_pct == 200.0 and first.minutes_compared == 60
    assert first.clips_3h == 6 and first.top_clip_views == 250 and first.streamer_id == known.id
    assert second.login == "stabil" and second.growth_pct == 0.0 and second.clips_3h == 0
    assert first.score > second.score
    assert hype.latest(redis).entries[0].login == "aufsteiger"  # zwischengespeichert für das Dashboard


@respx.mock
def test_hype_job_and_scheduler(database, twitch_settings, redis):
    _token()
    respx.get(f"{API}/streams").mock(return_value=_streams(stream("1", "kanal", "s1", viewers=500)))
    respx.get(f"{API}/clips").mock(return_value=_clips())
    assert jobs.hype_scan_job() == {"scanned": 1, "in_range": 1, "listed": 1}
    names = {j.name: j for j in scheduler.build_periodic_jobs(twitch_settings)}
    assert names["hype_scan"].interval_seconds == twitch_settings.hype_refresh_minutes * 60


def test_hype_job_skips_without_twitch(settings, database, redis):
    assert jobs.hype_scan_job() == {"skipped": True}


# --- Dashboard ----------------------------------------------------------------------
def _seed(redis, *, user_id="77", login="aufsteiger"):
    entry = hype.HypeEntry(
        user_id,
        login,
        "Aufsteiger",
        "Valorant",
        "Ranked",
        viewers=900,
        started_at=utcnow().isoformat(),
        growth_pct=120.0,
        minutes_compared=60,
        clips_3h=4,
        clips_24h=9,
        top_clip_url="https://clips.twitch.tv/x",
        top_clip_title="Ace",
        top_clip_views=300,
    )
    hype.score(entry)
    result = hype.HypeResult(
        utcnow().isoformat(), 120, 40, [entry], {"language": "de", "min": 50, "max": 20000}
    )
    redis.set(hype.LATEST_KEY, result.to_json())


def test_page_lists_channels(admin_client, redis):
    assert "Noch keine Daten" in admin_client.get("/hype").text
    _seed(redis)
    page = admin_client.get("/hype").text
    assert "Aufsteiger" in page and "+120 % in 60 min" in page and "Zustimmung anfragen" in page
    assert "4 in 3 h" in page and "Ace" in page
    assert "Aufsteiger" not in admin_client.get("/hype?game=Fortnite").text


def test_request_consent_adds_channel_and_shows_messages(admin_client, db_session, twitch_settings, redis):
    _seed(redis)
    response = post_form(admin_client, "/hype/77/request", csrf_page="/hype")
    assert response.status_code == 303
    streamer = db_session.query(Streamer).filter_by(twitch_login="aufsteiger").one()
    assert streamer.twitch_user_id == "77" and streamer.mode == "public"  # Rechte erst nach Zustimmung
    assert response.headers["location"] == f"/streamers/{streamer.id}#zustimmung"
    page = admin_client.get(f"/streamers/{streamer.id}").text
    assert "/oauth/twitch/start?invite=" in page and "Darf ich aus deinen Streams" in page
    # Ein zweiter Klick legt keinen doppelten Streamer an
    post_form(admin_client, "/hype/77/request", csrf_page="/hype")
    assert db_session.query(Streamer).filter_by(twitch_login="aufsteiger").count() == 1


def test_watch_only_and_unknown_entry(admin_client, db_session, twitch_settings, redis):
    _seed(redis)
    assert post_form(admin_client, "/hype/77/watch", csrf_page="/hype").status_code == 303
    assert db_session.query(Streamer).filter_by(twitch_login="aufsteiger").one().mode == "public"
    assert post_form(admin_client, "/hype/999/request", csrf_page="/hype").status_code == 404


def test_viewer_cannot_request(client, make_user, redis):
    from tests.conftest import login

    make_user("leser", "viewer")
    login(client, "leser")
    _seed(redis)
    page = client.get("/hype").text
    assert "Aufsteiger" in page and 'action="/hype/77/request"' not in page
    assert post_form(client, "/hype/77/request", csrf_page="/hype").status_code == 403


@pytest.mark.parametrize("path", ["/hype"])
def test_requires_login(client, path):
    assert client.get(path, follow_redirects=False).status_code == 303
