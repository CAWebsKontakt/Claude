import json
from datetime import UTC, datetime, timedelta

import httpx
import respx

from app.models import ActivityLog, EventSubSubscription, Streamer, StreamSession
from app.services.twitch_monitor import (
    apply_stream_offline,
    apply_stream_online,
    attach_vod,
    poll_live_status,
    resolve_streamers,
    sync_eventsub,
)
from app.timeutil import utcnow
from tests import factories
from tests.twitch_payloads import API, AUTH, CALLBACK, stream, subscription, token_response, user, video


def _token():
    respx.post(f"{AUTH}/token").mock(return_value=httpx.Response(200, json=token_response()))


@respx.mock
def test_resolve_streamers(helix, db_session):
    _token()
    factories.streamer(db_session, "kanal_a")
    factories.streamer(db_session, "gibtsnicht")
    db_session.commit()
    respx.get(f"{API}/users").mock(
        return_value=httpx.Response(200, json={"data": [user("111", "kanal_a", "Kanal_A")]})
    )
    respx.get(f"{API}/channels").mock(
        return_value=httpx.Response(
            200, json={"data": [{"broadcaster_id": "111", "broadcaster_language": "de"}]}
        )
    )
    report = resolve_streamers(db_session, helix)
    assert report.resolved == ["kanal_a"] and report.not_found == ["gibtsnicht"]
    a = db_session.query(Streamer).filter_by(twitch_login="kanal_a").one()
    assert (a.twitch_user_id, a.display_name, a.broadcaster_language) == ("111", "Kanal_A", "de")
    assert "static-cdn.jtvnw.net" in a.profile_image_url
    missing = db_session.query(Streamer).filter_by(twitch_login="gibtsnicht").one()
    assert "nicht gefunden" in missing.last_error
    # Zweiter Lauf: keine doppelte Warnung
    resolve_streamers(db_session, helix)
    assert db_session.query(ActivityLog).filter(ActivityLog.message.contains("nicht gefunden")).count() == 1


@respx.mock
def test_resolve_detects_renamed_login(helix, db_session):
    _token()
    factories.streamer(db_session, "altername", twitch_user_id="111")
    db_session.commit()
    respx.get(f"{API}/users").mock(
        return_value=httpx.Response(200, json={"data": [user("111", "neuername")]})
    )
    respx.get(f"{API}/channels").mock(return_value=httpx.Response(200, json={"data": []}))
    report = resolve_streamers(db_session, helix, refresh_known=True)
    assert report.renamed == [("altername", "neuername")]
    assert db_session.query(Streamer).one().twitch_login == "neuername"


@respx.mock
def test_poll_live_status_lifecycle(helix, db_session):
    _token()
    s = factories.streamer(db_session, "kanal", twitch_user_id="111")
    paused = factories.streamer(db_session, "pausiert", twitch_user_id="222", is_active=False)
    db_session.commit()
    route = respx.get(f"{API}/streams")
    started = datetime.now(UTC) - timedelta(minutes=20)

    route.mock(
        return_value=httpx.Response(
            200, json={"data": [stream("111", "kanal", "5001", viewers=100, started=started)]}
        )
    )
    report = poll_live_status(db_session, helix)
    assert report.went_live == [s.id] and report.checked == 1
    assert route.calls.last.request.url.params.get_list("user_id") == ["111"]  # pausierte nicht abgefragt
    assert s.is_live and s.live_title == "Ranked Grind" and s.live_viewer_count == 100
    session = db_session.query(StreamSession).one()
    assert session.twitch_stream_id == "5001" and session.peak_viewers == 100

    route.mock(
        return_value=httpx.Response(
            200, json={"data": [stream("111", "kanal", "5001", viewers=250, started=started)]}
        )
    )
    report = poll_live_status(db_session, helix)
    assert report.went_live == [] and db_session.query(StreamSession).count() == 1
    assert session.peak_viewers == 250

    route.mock(return_value=httpx.Response(200, json={"data": []}))
    report = poll_live_status(db_session, helix)
    assert report.ended_sessions == [session.id]
    assert not s.is_live and session.ended_at is not None
    assert not paused.is_live

    # Veraltete Info zum beendeten Stream darf ihn nicht wieder öffnen
    route.mock(
        return_value=httpx.Response(200, json={"data": [stream("111", "kanal", "5001", started=started)]})
    )
    poll_live_status(db_session, helix)
    assert not s.is_live and db_session.query(StreamSession).count() == 1


@respx.mock
def test_grace_period_after_eventsub_online(helix, db_session):
    _token()
    s = factories.streamer(db_session, "kanal", twitch_user_id="111")
    apply_stream_online(
        db_session, s, stream_id="7001", started_at=utcnow() - timedelta(seconds=30), source="EventSub"
    )
    db_session.commit()
    respx.get(f"{API}/streams").mock(return_value=httpx.Response(200, json={"data": []}))
    poll_live_status(db_session, helix)
    assert s.is_live, "Get Streams liefert neue Streams verzögert – kein Flackern"


def test_missed_offline_closes_stale_session(db_session):
    s = factories.streamer(db_session, "kanal", twitch_user_id="111")
    apply_stream_online(db_session, s, stream_id="1", started_at=utcnow() - timedelta(hours=5), source="test")
    apply_stream_online(db_session, s, stream_id="2", started_at=utcnow(), source="test")
    sessions = {x.twitch_stream_id: x for x in db_session.query(StreamSession).all()}
    assert sessions["1"].ended_at is not None and sessions["2"].ended_at is None
    closed = apply_stream_offline(db_session, s, source="test")
    assert [c.twitch_stream_id for c in closed] == ["2"]


@respx.mock
def test_attach_vod(helix, db_session):
    _token()
    s = factories.streamer(db_session, "kanal", twitch_user_id="111")
    session = StreamSession(
        streamer_id=s.id, twitch_stream_id="5001", started_at=utcnow(), ended_at=utcnow(), status="ended"
    )
    db_session.add(session)
    db_session.commit()
    route = respx.get(f"{API}/videos").mock(
        return_value=httpx.Response(
            200, json={"data": [video("v-old", "111", "4000"), video("v-new", "111", "5001")]}
        )
    )
    assert attach_vod(db_session, helix, session) == "v-new"
    assert route.calls.last.request.url.params["type"] == "archive"
    assert attach_vod(db_session, helix, session) == "v-new"
    assert route.call_count == 1  # bereits zugeordnet


@respx.mock
def test_sync_eventsub(helix, twitch_settings, db_session):
    _token()
    a = factories.streamer(db_session, "kanal_a", twitch_user_id="111")
    factories.streamer(db_session, "kanal_b", twitch_user_id="222")
    factories.streamer(db_session, "pausiert", twitch_user_id="333", is_active=False)
    db_session.commit()
    existing = [
        subscription("ok-1", "stream.online", "111"),  # behalten
        subscription("dup-1", "stream.online", "111"),  # Duplikat -> löschen
        subscription(
            "bad-1", "stream.offline", "111", status="notification_failures_exceeded"
        ),  # neu anlegen
        subscription(
            "old-cb", "stream.online", "222", callback="https://alte-domain.example/cb"
        ),  # falsche URL
        subscription("paused-1", "stream.online", "333"),  # pausierter Kanal -> löschen
        subscription("other", "channel.follow", "111"),  # fremder Typ -> unberührt
    ]
    respx.get(f"{API}/eventsub/subscriptions").mock(
        return_value=httpx.Response(
            200,
            json={"data": existing, "total": 6, "total_cost": 5, "max_total_cost": 10000, "pagination": {}},
        )
    )
    deleted = respx.delete(f"{API}/eventsub/subscriptions").mock(return_value=httpx.Response(204))
    created_ids = iter(["new-1", "new-2", "new-3", "new-revoke"])
    conditions = []

    def create(request):
        data = json.loads(request.content)
        assert data["transport"] == {
            "method": "webhook",
            "callback": CALLBACK,
            "secret": twitch_settings.twitch_eventsub_secret.get_secret_value(),
        }
        conditions.append((data["type"], data["condition"]))
        sub = subscription(
            next(created_ids),
            data["type"],
            data["condition"].get("broadcaster_user_id", ""),
            status="webhook_callback_verification_pending",
        )
        return httpx.Response(202, json={"data": [sub], "total": 4, "total_cost": 4, "max_total_cost": 10000})

    created = respx.post(f"{API}/eventsub/subscriptions").mock(side_effect=create)
    report = sync_eventsub(db_session, helix, twitch_settings)

    deleted_ids = {call.request.url.params["id"] for call in deleted.calls}
    assert deleted_ids == {"dup-1", "bad-1", "old-cb", "paused-1"}
    # 111 offline, 222 online+offline und einmal user.authorization.revoke für die Client-ID
    assert report.created == 4 and created.call_count == 4
    assert ("user.authorization.revoke", {"client_id": "test-client-id"}) in conditions
    assert report.kept == 1 and not report.errors
    mirrored = db_session.query(EventSubSubscription).all()
    assert len(mirrored) == 4
    assert {m.streamer_id for m in mirrored if m.type == "stream.online"} == {a.id, a.id + 1}


@respx.mock
def test_sync_eventsub_respects_cost_limit(helix, twitch_settings, db_session):
    _token()
    factories.streamer(db_session, "kanal_a", twitch_user_id="111")
    db_session.commit()
    respx.get(f"{API}/eventsub/subscriptions").mock(
        return_value=httpx.Response(
            200, json={"data": [], "total": 0, "total_cost": 10000, "max_total_cost": 10000}
        )
    )
    revoke_sub = subscription("rv", "user.authorization.revoke", "", cost=0)
    created = respx.post(f"{API}/eventsub/subscriptions").mock(
        return_value=httpx.Response(
            202, json={"data": [revoke_sub], "total_cost": 10000, "max_total_cost": 10000}
        )
    )
    report = sync_eventsub(db_session, helix, twitch_settings)
    # Keine kostenpflichtigen Kanal-Abos mehr – nur das (kostenlose) Widerrufs-Abo der App
    assert created.call_count == 1
    assert json.loads(created.calls[0].request.content)["type"] == "user.authorization.revoke"
    assert "Kostenlimit" in report.errors[0]


@respx.mock
def test_sync_without_public_https_deletes_managed_subscriptions(
    helix, twitch_settings, db_session, monkeypatch
):
    _token()
    monkeypatch.setattr(twitch_settings, "app_base_url", "http://localhost:8000")
    assert not twitch_settings.eventsub_webhooks_active
    factories.streamer(db_session, "kanal_a", twitch_user_id="111")
    db_session.commit()
    respx.get(f"{API}/eventsub/subscriptions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [subscription("s1", "stream.online", "111")],
                "total_cost": 1,
                "max_total_cost": 10000,
            },
        )
    )
    deleted = respx.delete(f"{API}/eventsub/subscriptions").mock(return_value=httpx.Response(204))
    created = respx.post(f"{API}/eventsub/subscriptions")
    report = sync_eventsub(db_session, helix, twitch_settings)
    assert deleted.call_count == 1 and created.call_count == 0
    assert report.webhooks_active is False
