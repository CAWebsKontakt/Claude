import json
import re
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from sqlalchemy import text

from app.enums import RightsStatus
from app.integrations.twitch.errors import TwitchAuthError
from app.models import ActivityLog, OAuthToken, Streamer
from app.services import twitch_authorization as auth
from app.services.streamers import compute_rights
from app.timeutil import utcnow
from tests import factories
from tests.conftest import post_form
from tests.twitch_payloads import AUTH, eventsub_headers, user_token_response, validate_response


def _streamer_with_permission(db, settings, login="partner", uid="111"):
    s = factories.authorized_streamer(db, settings, login, twitch_user_id=uid)
    # Token entfernen: Ausgangslage "Erlaubnis da, OAuth fehlt"
    for token in list(s.oauth_tokens):
        db.delete(token)
    db.commit()
    db.refresh(s)
    assert compute_rights(s).status == RightsStatus.MISSING_OAUTH
    return s


def _invite_token(url: str) -> str:
    return parse_qs(urlparse(url).query)["invite"][0]


def test_invite_requires_documented_permission(db_session, twitch_settings, make_user):
    admin = make_user()
    public = factories.streamer(db_session, "oeffentlich", twitch_user_id="5")
    with pytest.raises(auth.AuthorizationError, match="Erlaubnis"):
        auth.create_invite(db_session, twitch_settings, public, actor=admin, include_chat=False)
    s = _streamer_with_permission(db_session, twitch_settings)
    url = auth.create_invite(db_session, twitch_settings, s, actor=admin, include_chat=True)
    assert url.startswith("https://clips.example.com/oauth/twitch/start?invite=")


def test_invite_expiry_and_tampering(db_session, twitch_settings, make_user, redis, monkeypatch):
    admin = make_user()
    s = _streamer_with_permission(db_session, twitch_settings)
    import itsdangerous.timed

    real_time = time.time
    monkeypatch.setattr(itsdangerous.timed.time, "time", lambda: real_time() - 8 * 86400)
    old = _invite_token(auth.create_invite(db_session, twitch_settings, s, actor=admin, include_chat=False))
    monkeypatch.setattr(itsdangerous.timed.time, "time", real_time)
    with pytest.raises(auth.AuthorizationError, match="abgelaufen"):
        auth.read_invite(twitch_settings, redis, old)
    fresh = _invite_token(auth.create_invite(db_session, twitch_settings, s, actor=admin, include_chat=False))
    assert auth.read_invite(twitch_settings, redis, fresh).streamer_id == s.id
    with pytest.raises(auth.AuthorizationError, match="ungültig"):
        auth.read_invite(twitch_settings, redis, fresh[:-3] + "abc")


@respx.mock
def test_full_oauth_flow_as_broadcaster(client, db_session, twitch_settings, make_user, redis):
    admin = make_user()
    s = _streamer_with_permission(db_session, twitch_settings)
    invite = _invite_token(auth.create_invite(db_session, twitch_settings, s, actor=admin, include_chat=True))
    db_session.commit()

    consent = client.get(f"/oauth/twitch/start?invite={invite}")
    assert consent.status_code == 200
    assert "channel:manage:clips" in consent.text and "user:read:chat" in consent.text
    assert "Widerruf jederzeit" in consent.text

    redirect = client.get(f"/oauth/twitch/authorize?invite={invite}", follow_redirects=False)
    assert redirect.status_code == 303
    target = urlparse(redirect.headers["location"])
    params = parse_qs(target.query)
    assert f"{target.scheme}://{target.netloc}{target.path}" == f"{AUTH}/authorize"
    assert params["redirect_uri"] == ["https://clips.example.com/oauth/twitch/callback"]
    assert params["force_verify"] == ["true"] and params["response_type"] == ["code"]
    assert "channel:manage:clips" in params["scope"][0] and "user:bot" in params["scope"][0]
    state = params["state"][0]

    token_route = respx.post(f"{AUTH}/token").mock(
        return_value=httpx.Response(
            200,
            json=user_token_response(
                scopes=[
                    "channel:manage:clips",
                    "editor:manage:clips",
                    "user:read:chat",
                    "user:bot",
                    "channel:bot",
                ]
            ),
        )
    )
    respx.get(f"{AUTH}/validate").mock(
        return_value=httpx.Response(
            200,
            json=validate_response(
                user_id="111",
                scopes=[
                    "channel:manage:clips",
                    "editor:manage:clips",
                    "user:read:chat",
                    "user:bot",
                    "channel:bot",
                ],
            ),
        )
    )
    result = client.get(f"/oauth/twitch/callback?code=abc123&scope=x&state={state}")
    assert result.status_code == 200 and "Zugriff ist eingerichtet" in result.text
    assert "Kanalinhaber" in result.text and "erlaubt (nur Zählwerte)" in result.text
    form = parse_qs(token_route.calls.last.request.content.decode())
    assert form["grant_type"] == ["authorization_code"] and form["code"] == ["abc123"]

    db_session.expire_all()
    token = db_session.query(OAuthToken).one()
    assert token.role == "broadcaster" and token.access_token == "user-access-new"
    raw = db_session.execute(text("SELECT access_token FROM oauth_tokens")).scalar_one()
    assert "user-access-new" not in raw
    assert compute_rights(db_session.get(Streamer, s.id)).status == RightsStatus.READY_PUBLISHING

    # Link ist verbraucht, State ebenfalls
    again = client.get(f"/oauth/twitch/start?invite={invite}")
    assert "bereits verwendet" in again.text
    replay = client.get(f"/oauth/twitch/callback?code=abc123&state={state}")
    assert "abgelaufen oder ungültig" in replay.text


@respx.mock
def test_oauth_as_editor_and_missing_scope(client, db_session, twitch_settings, make_user, redis):
    admin = make_user()
    s = _streamer_with_permission(db_session, twitch_settings)
    db_session.commit()

    def run(scopes, user_id):
        invite = _invite_token(
            auth.create_invite(db_session, twitch_settings, s, actor=admin, include_chat=False)
        )
        db_session.commit()
        location = client.get(f"/oauth/twitch/authorize?invite={invite}", follow_redirects=False).headers[
            "location"
        ]
        state = parse_qs(urlparse(location).query)["state"][0]
        respx.post(f"{AUTH}/token").mock(
            return_value=httpx.Response(200, json=user_token_response(scopes=scopes))
        )
        respx.get(f"{AUTH}/validate").mock(
            return_value=httpx.Response(
                200, json=validate_response(user_id=user_id, login="editor1", scopes=scopes)
            )
        )
        return client.get(f"/oauth/twitch/callback?code=c&state={state}")

    revoke = respx.post(f"{AUTH}/revoke").mock(return_value=httpx.Response(200))
    denied = run(["user:read:email"], "999")
    assert "Berechtigung für Clips wurde nicht erteilt" in denied.text and revoke.call_count == 1

    ok = run(["editor:manage:clips"], "999")
    assert "Editor" in ok.text
    db_session.expire_all()
    token = db_session.query(OAuthToken).one()
    assert token.role == "editor" and token.external_user_id == "999"
    assert (
        db_session.query(ActivityLog).filter(ActivityLog.message.contains("nicht der Kanalinhaber")).count()
        == 1
    )


def test_callback_denied_and_invalid_state(client, twitch_settings):
    assert "abgelehnt" in client.get("/oauth/twitch/callback?error=access_denied&state=x").text
    assert "abgelaufen oder ungültig" in client.get("/oauth/twitch/callback?code=x&state=unbekannt").text


@respx.mock
def test_token_refresh_and_rejection(db_session, twitch_settings, make_user):
    make_user()
    s = factories.authorized_streamer(db_session, twitch_settings, expires_in_minutes=1)
    token = s.oauth_tokens[0]
    refresh = respx.post(f"{AUTH}/token").mock(
        return_value=httpx.Response(200, json=user_token_response(access="erneuert"))
    )
    with httpx.Client() as http:
        credential = auth.credential_for(db_session, twitch_settings, http, token)
    assert credential.access_token == "erneuert" and token.refresh_token == "user-refresh-new"
    assert parse_qs(refresh.calls.last.request.content.decode())["grant_type"] == ["refresh_token"]

    token.expires_at = utcnow()
    respx.post(f"{AUTH}/token").mock(
        return_value=httpx.Response(400, json={"message": "Invalid refresh token"})
    )
    with httpx.Client() as http, pytest.raises(TwitchAuthError):
        auth.credential_for(db_session, twitch_settings, http, token)
    assert token.revoked_at is not None
    assert compute_rights(s).status == RightsStatus.MISSING_OAUTH


@respx.mock
def test_call_with_user_token_retries_after_401(db_session, twitch_settings, make_user):
    make_user()
    s = factories.authorized_streamer(db_session, twitch_settings)
    respx.post(f"{AUTH}/token").mock(return_value=httpx.Response(200, json=user_token_response(access="neu")))
    seen = []

    def action(credential):
        seen.append(credential.access_token)
        if len(seen) == 1:
            raise TwitchAuthError("401")
        return "ok"

    with httpx.Client() as http:
        assert auth.call_with_user_token(db_session, twitch_settings, http, s.oauth_tokens[0], action) == "ok"
    assert seen == ["user-access-1", "neu"]


@respx.mock
def test_validate_all_tokens(db_session, twitch_settings, make_user):
    make_user()
    good = factories.authorized_streamer(db_session, twitch_settings, "gut", twitch_user_id="1")
    bad = factories.authorized_streamer(db_session, twitch_settings, "schlecht", twitch_user_id="2")
    bad.oauth_tokens[0].access_token = "kaputt"
    db_session.flush()

    def validate(request):
        if request.headers["Authorization"] == "OAuth kaputt":
            return httpx.Response(401, json={"message": "invalid access token"})
        return httpx.Response(200, json=validate_response(user_id="1"))

    respx.get(f"{AUTH}/validate").mock(side_effect=validate)
    respx.post(f"{AUTH}/token").mock(
        return_value=httpx.Response(400, json={"message": "Invalid refresh token"})
    )
    with httpx.Client() as http:
        report = auth.validate_all_tokens(db_session, twitch_settings, http)
    assert (report.valid, report.revoked) == (1, 1)
    assert good.oauth_tokens[0].last_validated_at is not None
    assert bad.oauth_tokens[0].revoked_at is not None


def test_eventsub_user_revoke_stops_processing(client, db_session, twitch_settings, make_user):
    make_user()
    s = factories.authorized_streamer(db_session, twitch_settings, "partner", twitch_user_id="111")
    db_session.commit()
    body = json.dumps(
        {
            "subscription": {
                "id": "rv",
                "type": "user.authorization.revoke",
                "status": "enabled",
                "condition": {"client_id": "test-client-id"},
            },
            "event": {
                "client_id": "test-client-id",
                "user_id": "111",
                "user_login": "partner",
                "user_name": "Partner",
            },
        }
    ).encode()
    response = client.post(
        "/webhooks/twitch/eventsub",
        content=body,
        headers=eventsub_headers(body, "notification", message_id="rv-1"),
    )
    assert response.status_code == 204
    db_session.expire_all()
    assert compute_rights(db_session.get(Streamer, s.id)).status == RightsStatus.MISSING_OAUTH


@respx.mock
def test_dashboard_invite_and_disconnect(admin_client, db_session, twitch_settings):
    s = factories.authorized_streamer(db_session, twitch_settings)
    db_session.commit()
    post_form(admin_client, f"/streamers/{s.id}/twitch-invite", {"include_chat": "1"})
    page = admin_client.get(f"/streamers/{s.id}").text
    match = re.search(r'value="(https://clips\.example\.com/oauth/twitch/start\?invite=[^"]+)"', page)
    assert match, "Autorisierungslink wird angezeigt"
    assert "Kanalinhaber" in page and "Trennen" in page

    revoke = respx.post(f"{AUTH}/revoke").mock(return_value=httpx.Response(200))
    token_id = s.oauth_tokens[0].id
    post_form(admin_client, f"/streamers/{s.id}/tokens/{token_id}/disconnect")
    assert revoke.call_count == 1
    db_session.expire_all()
    assert db_session.get(OAuthToken, token_id).revoked_at is not None
    assert "getrennt" in admin_client.get(f"/streamers/{s.id}").text


def test_expired_token_window_constant():
    assert timedelta(minutes=5) == auth.REFRESH_MARGIN
