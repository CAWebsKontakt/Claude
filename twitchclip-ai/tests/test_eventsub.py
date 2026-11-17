import json
from datetime import UTC, datetime, timedelta

import pytest
from rq.registry import ScheduledJobRegistry

from app.integrations.twitch.eventsub import EventSubVerificationError, verify_and_parse
from app.models import ActivityLog, EventSubSubscription, Streamer, StreamSession
from app.workers.queues import QUEUE_DEFAULT, get_queue
from tests import factories
from tests.twitch_payloads import (
    EVENTSUB_SECRET,
    eventsub_headers,
    offline_notification,
    online_notification,
)

URL = "/webhooks/twitch/eventsub"


def test_signature_verification():
    body = online_notification("1337", "kanal")
    headers = eventsub_headers(body, "notification")
    message = verify_and_parse(headers, body, EVENTSUB_SECRET)
    assert message.subscription_type == "stream.online"
    assert message.event["broadcaster_user_id"] == "1337"

    with pytest.raises(EventSubVerificationError, match="Signatur"):
        verify_and_parse(headers, body + b" ", EVENTSUB_SECRET)
    with pytest.raises(EventSubVerificationError, match="Signatur"):
        verify_and_parse(headers, body, "anderes-secret-123")
    with pytest.raises(EventSubVerificationError, match="Header"):
        verify_and_parse({"Content-Type": "application/json"}, body, EVENTSUB_SECRET)
    old = eventsub_headers(body, "notification", timestamp=datetime.now(UTC) - timedelta(minutes=11))
    with pytest.raises(EventSubVerificationError, match="alt"):
        verify_and_parse(old, body, EVENTSUB_SECRET)


def _post(client, body: bytes, message_type: str, message_id: str = "msg-1", **kwargs):
    return client.post(
        URL, content=body, headers=eventsub_headers(body, message_type, message_id=message_id, **kwargs)
    )


def test_verification_challenge(client, twitch_settings, db_session):
    streamer = factories.streamer(db_session, "kanal", twitch_user_id="1337")
    db_session.add(
        EventSubSubscription(
            streamer_id=streamer.id,
            twitch_subscription_id="s",
            type="stream.online",
            status="webhook_callback_verification_pending",
            cost=1,
        )
    )
    db_session.commit()
    body = json.dumps(
        {
            "challenge": "pogchamp-kappa-360noscope-vohiyo",
            "subscription": {"id": "s", "type": "stream.online"},
        }
    ).encode()
    response = _post(client, body, "webhook_callback_verification")
    assert response.status_code == 200
    assert response.text == "pogchamp-kappa-360noscope-vohiyo"
    assert response.headers["content-type"].startswith("text/plain")
    db_session.expire_all()
    assert db_session.query(EventSubSubscription).one().status == "enabled"


def test_bad_signature_rejected(client, twitch_settings):
    body = online_notification("1337", "kanal")
    headers = eventsub_headers(body, "notification", secret="falsches-secret-000")
    assert client.post(URL, content=body, headers=headers).status_code == 403


def test_stream_online_and_offline_flow(client, twitch_settings, db_session, redis):
    streamer = factories.streamer(db_session, "kanal", twitch_user_id="1337")
    db_session.commit()

    assert (
        _post(client, online_notification("1337", "kanal", "9001"), "notification", "m-on").status_code == 204
    )
    db_session.expire_all()
    streamer = db_session.get(Streamer, streamer.id)
    assert streamer.is_live
    session = db_session.query(StreamSession).one()
    assert session.twitch_stream_id == "9001" and session.ended_at is None
    scheduled = ScheduledJobRegistry(queue=get_queue(QUEUE_DEFAULT, redis)).get_job_ids()
    assert any(job_id.startswith("twitch-details") for job_id in scheduled)

    # Duplikat derselben Nachricht wird ignoriert
    assert (
        _post(client, online_notification("1337", "kanal", "9001"), "notification", "m-on").status_code == 204
    )
    assert db_session.query(ActivityLog).filter(ActivityLog.message.contains("ist live")).count() == 1

    assert _post(client, offline_notification("1337", "kanal"), "notification", "m-off").status_code == 204
    db_session.expire_all()
    assert not db_session.get(Streamer, streamer.id).is_live
    session = db_session.query(StreamSession).one()
    assert session.ended_at is not None and session.status == "ended"
    scheduled = ScheduledJobRegistry(queue=get_queue(QUEUE_DEFAULT, redis)).get_job_ids()
    assert f"twitch-vod-{session.id}" in scheduled
    assert f"twitch-clips-after-stream-{streamer.id}" in scheduled


def test_unknown_broadcaster_is_ignored(client, twitch_settings, db_session):
    assert _post(client, online_notification("424242", "fremd"), "notification", "m-x").status_code == 204
    assert db_session.query(StreamSession).count() == 0


def test_revocation_marks_subscription(client, twitch_settings, db_session):
    streamer = factories.streamer(db_session, "kanal", twitch_user_id="1337")
    db_session.add(
        EventSubSubscription(
            streamer_id=streamer.id,
            twitch_subscription_id="sub-on",
            type="stream.online",
            status="enabled",
            cost=1,
        )
    )
    db_session.commit()
    body = json.dumps(
        {
            "subscription": {
                "id": "sub-on",
                "type": "stream.online",
                "status": "authorization_revoked",
                "condition": {"broadcaster_user_id": "1337"},
            }
        }
    ).encode()
    assert _post(client, body, "revocation", "m-rev").status_code == 204
    db_session.expire_all()
    assert db_session.query(EventSubSubscription).one().status == "authorization_revoked"
    assert db_session.query(ActivityLog).filter(ActivityLog.message.contains("widerrufen")).count() == 1


def test_subscription_type_falls_back_to_header():
    body = json.dumps({"subscription": None, "event": {"broadcaster_user_id": "1"}}).encode()
    headers = eventsub_headers(body, "notification")
    headers["Twitch-Eventsub-Subscription-Type"] = "stream.offline"
    assert verify_and_parse(headers, body, EVENTSUB_SECRET).subscription_type == "stream.offline"
