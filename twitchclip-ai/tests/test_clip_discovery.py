from datetime import UTC, datetime, timedelta

import httpx
import respx

from app.models import Streamer, TwitchClip
from app.services.clip_discovery import discover_clips_for_streamer, prune_old_clips, time_slices
from app.services.clip_scoring import ClipFeatures, cluster_counts, duration_fit, score_clips
from tests import factories
from tests.twitch_payloads import API, AUTH, clip, token_response

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def features(key, views, hours_ago, duration=30.0, featured=False, video="v1", offset=None):
    return ClipFeatures(key, views, NOW - timedelta(hours=hours_ago), duration, featured, video, offset)


def test_time_slices_cover_window():
    slices = time_slices(NOW - timedelta(hours=30), NOW, hours=12)
    assert len(slices) == 3
    assert slices[0][0] == NOW - timedelta(hours=30) and slices[-1][1] == NOW
    assert all(a < b for a, b in slices)


def test_duration_fit():
    assert duration_fit(30, 20, 45) == 1.0
    assert 0 < duration_fit(10, 20, 45) < 1
    assert duration_fit(60, 20, 45) == 0.0


def test_cluster_counts_same_moment():
    clips = [
        features("a", 10, 1, offset=1000),
        features("b", 10, 1, offset=1040),
        features("c", 10, 1, offset=5000),
        features("d", 10, 1, video="v2", offset=1010),
    ]
    counts = cluster_counts(clips)
    assert counts == {"a": 1, "b": 1, "c": 0, "d": 0}


def test_scores_are_relative_and_bounded():
    clips = [
        features("viral", 5000, 2, offset=100),
        features("viral-2", 300, 2, offset=120),  # selber Moment
        features("solide", 400, 10),
        features("alt", 400, 70),
        features("lahm", 5, 20, duration=58),
    ]
    scores = score_clips(clips, now=NOW, duration_min=20, duration_max=45)
    values = {k: v[0] for k, v in scores.items()}
    assert all(0 <= v <= 100 for v in values.values())
    assert values["viral"] > values["solide"] > values["alt"] > values["lahm"]
    assert scores["viral"][1]["aehnliche_clips"] == 1
    assert scores["lahm"][1]["laenge"] < 1


@respx.mock
def test_discovery_upserts_and_recommends(helix, twitch_settings, db_session):
    respx.post(f"{AUTH}/token").mock(return_value=httpx.Response(200, json=token_response()))
    s = factories.streamer(db_session, "kanal", twitch_user_id="111")
    dismissed = factories.twitch_clip(db_session, s, clip_id="Dismissed", status="dismissed")
    db_session.commit()
    now = datetime.now(UTC)
    payload = [
        clip("Top", "111", views=9000, created=now - timedelta(hours=1), vod_offset=500),
        clip("Top2", "111", views=800, created=now - timedelta(hours=1), vod_offset=520),
        clip("Mittel", "111", views=200, created=now - timedelta(hours=20)),
        clip("Dismissed", "111", views=50000, created=now - timedelta(hours=1)),
        clip("Klein", "111", views=2, created=now - timedelta(hours=40), duration=59),
    ]
    route = respx.get(f"{API}/clips").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={
                "data": payload
                if request.url.params["started_at"]
                <= payload[0]["created_at"]
                <= request.url.params["ended_at"]
                else [],
                "pagination": {},
            },
        )
    )
    report = discover_clips_for_streamer(db_session, helix, twitch_settings, s)
    assert route.call_count == 4  # 48 h in 12-h-Fenstern
    assert report.new == 4 and report.updated == 1
    rows = {c.twitch_clip_id: c for c in db_session.query(TwitchClip).all()}
    assert rows["Top"].status == "recommended" and rows["Top"].score_breakdown["aehnliche_clips"] == 1
    assert rows["Klein"].status == "new"
    assert rows["Dismissed"].status == "dismissed" and rows["Dismissed"].view_count == 50000
    assert rows["Top"].discovery_score > rows["Mittel"].discovery_score > rows["Klein"].discovery_score
    assert db_session.get(Streamer, s.id).last_clip_scan_at is not None
    assert dismissed.id == rows["Dismissed"].id

    # Zweiter Lauf: Aufrufe aktualisiert, keine Duplikate
    payload[0]["view_count"] = 12000
    report = discover_clips_for_streamer(db_session, helix, twitch_settings, s)
    assert report.new == 0 and report.updated == 5
    assert db_session.query(TwitchClip).count() == 5
    assert rows["Top"].view_count == 12000


def test_prune_old_clips(db_session):
    s = factories.streamer(db_session, "kanal")
    factories.twitch_clip(
        db_session, s, clip_id="alt", clip_created_at=datetime.now(UTC) - timedelta(days=40)
    )
    factories.twitch_clip(db_session, s, clip_id="neu")
    db_session.commit()
    assert prune_old_clips(db_session, 30) == 1
    assert [c.twitch_clip_id for c in db_session.query(TwitchClip).all()] == ["neu"]
