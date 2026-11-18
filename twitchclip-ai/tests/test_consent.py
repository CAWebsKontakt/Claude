"""Ein-Klick-Zustimmung von Streamern (Zustimmungslink) und Anfrage-Vorlagen."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app.enums import RightsStatus
from app.models import ActivityLog, OAuthToken, Streamer, StreamerPermission
from app.services import twitch_authorization as auth
from app.services.outreach import WHISPER_LIMIT, build_messages
from app.services.streamers import compute_rights
from tests import factories
from tests.conftest import post_form
from tests.twitch_payloads import AUTH, user_token_response, validate_response


def _token(url: str) -> str:
    return parse_qs(urlparse(url).query)["invite"][0]


def _public_streamer(db, login="neuer_kanal", uid="111"):
    s = factories.streamer(db, login, twitch_user_id=uid, display_name="Neuer Kanal")
    db.commit()
    assert compute_rights(s).status == RightsStatus.PUBLIC_ONLY
    return s


def _login_at_twitch(client, invite, *, query: str, user_id="111", login="neuer_kanal"):
    """Zustimmungsseite absenden → Twitch-Anmeldung (gemockt) → Callback."""
    location = client.get(f"/oauth/twitch/authorize?invite={invite}&{query}", follow_redirects=False)
    assert location.status_code == 303
    target = parse_qs(urlparse(location.headers["location"]).query)
    state = target["state"][0]
    scopes = target["scope"][0].split()
    respx.post(f"{AUTH}/token").mock(
        return_value=httpx.Response(200, json=user_token_response(scopes=scopes))
    )
    respx.get(f"{AUTH}/validate").mock(
        return_value=httpx.Response(200, json=validate_response(user_id=user_id, login=login, scopes=scopes))
    )
    return client.get(f"/oauth/twitch/callback?code=c&state={state}"), scopes


def test_consent_invite_needs_twitch_and_linked_account(db_session, settings, twitch_settings, make_user):
    admin = make_user()
    unlinked = factories.streamer(db_session, "ohne_id")
    with pytest.raises(auth.AuthorizationError, match="verknüpft"):
        auth.create_consent_invite(
            db_session, twitch_settings, unlinked, actor=admin, offer_publishing=True, include_chat=False
        )
    demo = factories.streamer(db_session, "demo", twitch_user_id="9", is_demo=True)
    with pytest.raises(auth.AuthorizationError, match="Demo"):
        auth.create_consent_invite(
            db_session, twitch_settings, demo, actor=admin, offer_publishing=True, include_chat=False
        )
    # Kein dokumentierter Nachweis nötig – der entsteht erst durch die Zustimmung
    s = _public_streamer(db_session)
    url = auth.create_consent_invite(
        db_session, twitch_settings, s, actor=admin, offer_publishing=True, include_chat=False
    )
    invite = auth.read_invite(twitch_settings, __import__("fakeredis").FakeRedis(), _token(url))
    assert invite.consent and invite.offer_publishing and invite.actor_id == admin.id


@respx.mock
def test_broadcaster_consent_documents_permission_and_enables_mode_b(
    client, db_session, twitch_settings, make_user, redis
):
    admin = make_user()
    s = _public_streamer(db_session)
    invite = _token(
        auth.create_consent_invite(
            db_session, twitch_settings, s, actor=admin, offer_publishing=True, include_chat=True
        )
    )
    db_session.commit()

    page = client.get(f"/oauth/twitch/start?invite={invite}")
    assert page.status_code == 200
    assert "Ihre Zustimmung" in page.text and "Auf TikTok veröffentlichen" in page.text
    assert 'name="publish" value="1">' in page.text  # nicht vorausgewählt
    assert "Fassung 2026-11-v1" in page.text

    # Ohne Haken bei der Zustimmung geht es nicht weiter
    missing = client.get(f"/oauth/twitch/authorize?invite={invite}", follow_redirects=False)
    assert "fehlt=1" in missing.headers["location"]
    assert "Bitte setzen Sie den Haken" in client.get(missing.headers["location"]).text

    result, scopes = _login_at_twitch(client, invite, query="agree=1&publish=1")
    assert "user:read:chat" not in scopes  # Chat nicht angehakt → nicht angefragt
    assert result.status_code == 200 and "Veröffentlichung auf TikTok" in result.text

    db_session.expire_all()
    s = db_session.get(Streamer, s.id)
    rights = compute_rights(s)
    assert s.mode == "authorized" and rights.status == RightsStatus.READY_PUBLISHING
    permission = rights.permission
    assert permission.granted_by_role == "broadcaster" and permission.granted_by_name == "neuer_kanal"
    assert (
        "Online-Zustimmung" in permission.evidence_reference
        and "Twitch-ID 111" in permission.evidence_reference
    )
    assert "auf TikTok veröffentlicht" in permission.scope_note and "Chat" not in permission.scope_note
    assert permission.recorded_by_user_id == admin.id
    assert (
        db_session.query(ActivityLog).filter(ActivityLog.message.contains("Online-Zustimmung")).count() == 1
    )


@respx.mock
def test_consent_without_publishing_and_narrowing_replaces_old_permission(
    client, db_session, twitch_settings, make_user, redis
):
    admin = make_user()
    s = _public_streamer(db_session)

    def consent(query):
        invite = _token(
            auth.create_consent_invite(
                db_session, twitch_settings, s, actor=admin, offer_publishing=True, include_chat=False
            )
        )
        db_session.commit()
        return _login_at_twitch(client, invite, query=query)[0]

    consent("agree=1&publish=1")
    consent("agree=1")  # Streamer nimmt die Veröffentlichung später zurück
    db_session.expire_all()
    s = db_session.get(Streamer, s.id)
    assert compute_rights(s).status == RightsStatus.READY_PROCESSING  # neueste Entscheidung gilt
    permissions = db_session.query(StreamerPermission).order_by(StreamerPermission.id).all()
    assert permissions[0].revoked_at is not None and "ersetzt" in permissions[0].revoked_reason
    assert permissions[1].revoked_at is None and not permissions[1].allows_publishing


@respx.mock
def test_only_channel_owner_can_consent(client, db_session, twitch_settings, make_user, redis):
    admin = make_user()
    s = _public_streamer(db_session)
    invite = _token(
        auth.create_consent_invite(
            db_session, twitch_settings, s, actor=admin, offer_publishing=True, include_chat=False
        )
    )
    db_session.commit()
    revoke = respx.post(f"{AUTH}/revoke").mock(return_value=httpx.Response(200))
    result, _ = _login_at_twitch(client, invite, query="agree=1&publish=1", user_id="999", login="moderator")
    assert "nur der Inhaber" in result.text and revoke.called
    db_session.expire_all()
    assert db_session.query(StreamerPermission).count() == 0
    assert db_session.query(OAuthToken).count() == 0
    assert db_session.get(Streamer, s.id).mode == "public"


def test_publishing_cannot_be_added_if_not_offered(db_session, twitch_settings, make_user, redis):
    admin = make_user()
    s = _public_streamer(db_session)
    invite = auth.read_invite(
        twitch_settings,
        redis,
        _token(
            auth.create_consent_invite(
                db_session, twitch_settings, s, actor=admin, offer_publishing=False, include_chat=False
            )
        ),
    )
    auth.begin_authorization(twitch_settings, redis, invite, auth.ConsentChoice(publishing=True, chat=True))
    stored = [redis.get(k) for k in redis.keys(f"{auth.STATE_PREFIX}*")]
    assert b'"p": false' in stored[0] and b'"c": false' in stored[0]


def test_dashboard_creates_link_with_message_templates(admin_client, db_session, twitch_settings):
    s = _public_streamer(db_session)
    page = admin_client.get(f"/streamers/{s.id}").text
    assert "Zustimmung anfragen" in page
    response = post_form(admin_client, f"/streamers/{s.id}/consent-invite", {"offer_publishing": "1"})
    assert response.status_code == 303
    page = admin_client.get(f"/streamers/{s.id}").text
    assert "/oauth/twitch/start?invite=" in page
    assert "Twitch-Flüsternachricht" in page and "Discord / E-Mail" in page and "Englisch" in page
    assert "Darf ich aus deinen Streams" in page
    # Link wird nur einmal angezeigt
    assert "/oauth/twitch/start?invite=" not in admin_client.get(f"/streamers/{s.id}").text


def test_message_templates():
    link = "https://clips.example.com/oauth/twitch/start?invite=" + "x" * 160
    messages = {
        m.key: m
        for m in build_messages(
            channel="kanal", display_name="Kanal", link=link, publishing=True, targets=["@meine_clips"]
        )
    }
    assert set(messages) == {"whisper", "long", "english", "follow_up"}
    for key in ("whisper", "english"):
        assert len(messages[key].text) <= WHISPER_LIMIT and messages[key].text.endswith(link)
    assert "@meine_clips" in messages["long"].text and "twitch.tv/kanal" in messages["long"].text
    assert "von Hand frei" in messages["long"].text
    very_long = link + "y" * 300
    whisper = build_messages(channel="k", display_name="K", link=very_long, publishing=True, targets=[])[0]
    assert whisper.text.endswith(very_long)  # Link wird nie abgeschnitten
    private = build_messages(channel="k", display_name="K", link=link, publishing=False, targets=[])
    assert "TikTok" not in private[0].text
