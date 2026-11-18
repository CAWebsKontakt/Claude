"""TikTok Login Kit, API-Client und Kontoverwaltung (Phase 6) – alle HTTP-Aufrufe gemockt (respx)."""

from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from sqlalchemy import select, text

from app.integrations.tiktok import oauth
from app.integrations.tiktok.client import MB, TikTokClient, plan_chunks
from app.integrations.tiktok.errors import TikTokAPIError, TikTokAuthError, TikTokRateLimitedError
from app.models import ActivityLog, OAuthToken, TikTokAccount
from app.services import tiktok_accounts
from app.timeutil import utcnow
from tests.conftest import post_form
from tests.tiktok_helpers import API, CREATOR_INFO, account, configure, envelope, token_response


# --- Chunk-Regeln der Content Posting API -----------------------------------------
@pytest.mark.parametrize(
    ("size", "preferred", "chunk", "count"),
    [
        (3 * MB, 10 * MB, 3 * MB, 1),  # < 5 MB → ein Stück in voller Größe
        (23 * MB, 10 * MB, 10 * MB, 2),  # Rest landet im letzten Stück (13 MB)
        (10 * MB, 10 * MB, 10 * MB, 1),
        (6 * MB, 2 * MB, 5 * MB, 1),  # Mindestgröße 5 MB
        (200 * MB, 64 * MB, 64 * MB, 3),
    ],
)
def test_chunk_plan_follows_tiktok_rules(size, preferred, chunk, count):
    plan = plan_chunks(size, preferred)
    assert (plan.chunk_size, plan.total_chunk_count) == (chunk, count)
    ranges = plan.ranges()
    assert ranges[0][0] == 0 and ranges[-1][1] == size - 1
    assert all(b[0] == a[1] + 1 for a, b in zip(ranges, ranges[1:], strict=False))
    assert ranges[-1][1] - ranges[-1][0] + 1 < 2 * chunk  # letztes Stück ≤ 128 MB
    assert plan.source_info() == {
        "source": "FILE_UPLOAD",
        "video_size": size,
        "chunk_size": chunk,
        "total_chunk_count": count,
    }


def test_chunk_plan_rejects_empty_file():
    with pytest.raises(ValueError):
        plan_chunks(0)


# --- OAuth mit PKCE ------------------------------------------------------------------
def test_authorize_url_uses_pkce_s256(settings, monkeypatch):
    configure(settings, monkeypatch)
    verifier, challenge = oauth.new_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected and 43 <= len(verifier) <= 128
    url = urlparse(oauth.authorize_url(settings, "state-1", challenge))
    params = parse_qs(url.query)
    assert url.netloc == "www.tiktok.com" and url.path == "/v2/auth/authorize/"
    assert params["client_key"] == ["tt-client-key"]
    assert params["scope"] == ["user.info.basic,video.upload,video.publish"]
    assert params["redirect_uri"] == ["https://testserver/oauth/tiktok/callback"]
    assert params["code_challenge_method"] == ["S256"] and params["code_challenge"] == [challenge]


def test_connect_flow_stores_encrypted_tokens_and_creator_info(
    admin_client, settings, monkeypatch, db_session, database
):
    configure(settings, monkeypatch)
    response = post_form(admin_client, "/oauth/tiktok/start", csrf_page="/tiktok")
    assert response.status_code == 303
    params = parse_qs(urlparse(response.headers["location"]).query)
    state = params["state"][0]

    with respx.mock(assert_all_called=True) as mock:
        token_route = mock.post(f"{API}/v2/oauth/token/").mock(
            return_value=httpx.Response(200, json=token_response())
        )
        mock.get(f"{API}/v2/user/info/").mock(
            return_value=envelope({"user": {"open_id": "open-123", "display_name": "Partner Clips"}})
        )
        mock.post(f"{API}/v2/post/publish/creator_info/query/").mock(return_value=envelope(CREATOR_INFO))
        callback = admin_client.get(
            f"/oauth/tiktok/callback?code=auth-code&state={state}", follow_redirects=False
        )
    assert callback.status_code == 303 and callback.headers["location"] == "/tiktok"
    body = parse_qs(token_route.calls[0].request.content.decode())
    assert body["grant_type"] == ["authorization_code"] and body["code"] == ["auth-code"]
    assert len(body["code_verifier"][0]) >= 43  # PKCE-Verifier wird mitgeschickt

    acc = db_session.scalar(select(TikTokAccount))
    assert acc.username == "partner_clips" and acc.is_active
    assert acc.creator_info["privacy_level_options"] == CREATOR_INFO["privacy_level_options"]
    token = tiktok_accounts.active_token(acc)
    assert token.access_token == "act.example-access" and "video.publish" in token.scopes
    with database.engine.connect() as conn:
        raw = conn.execute(text("SELECT access_token, refresh_token FROM oauth_tokens")).one()
    assert "act.example-access" not in raw[0] and "rft.example-refresh" not in raw[1]  # verschlüsselt

    page = admin_client.get("/tiktok").text
    assert "Partner Clips" in page and "Öffentlich" in page and "aktiv" in page


def test_callback_rejects_unknown_state_and_denied(admin_client, settings, monkeypatch, db_session):
    configure(settings, monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        token_route = mock.post(f"{API}/v2/oauth/token/")
        r = admin_client.get("/oauth/tiktok/callback?code=x&state=gefaelscht", follow_redirects=False)
        assert r.status_code == 303 and not token_route.called
    assert "abgelaufen" in admin_client.get("/tiktok").text
    admin_client.get("/oauth/tiktok/callback?error=access_denied&state=x", follow_redirects=False)
    assert "abgelehnt" in admin_client.get("/tiktok").text
    assert db_session.scalar(select(TikTokAccount)) is None


def test_connect_requires_configuration_and_editor(client, make_user, settings, monkeypatch):
    from tests.conftest import login

    make_user("leser", "viewer")
    login(client, "leser")
    assert "nicht konfiguriert" in client.get("/tiktok").text
    configure(settings, monkeypatch)
    assert post_form(client, "/oauth/tiktok/start", csrf_page="/tiktok").status_code == 403
    assert client.get("/oauth/tiktok/callback?code=a&state=b").status_code == 403


# --- API-Client: Fehlerbehandlung -------------------------------------------------------
@pytest.mark.parametrize(
    ("response", "error", "retryable"),
    [
        (httpx.Response(401, json={"error": {"code": "access_token_invalid"}}), TikTokAuthError, None),
        (envelope(code="rate_limit_exceeded", status=429), TikTokRateLimitedError, True),
        (httpx.Response(503, text="unavailable"), TikTokAPIError, True),
        (envelope(code="spam_risk_too_many_posts", status=403), TikTokAPIError, False),
    ],
)
def test_client_maps_errors(settings, response, error, retryable):
    with respx.mock, httpx.Client() as http:
        respx.post(f"{API}/v2/post/publish/creator_info/query/").mock(return_value=response)
        with pytest.raises(error) as info:
            TikTokClient(settings, http, "tok").creator_info()
    if retryable is not None:
        assert info.value.retryable is retryable
    if "spam_risk" in str(response.content):
        assert "Tageslimit" in str(info.value)  # verständliche deutsche Erklärung


def test_upload_sends_content_range_per_chunk(settings, tmp_path):
    path = tmp_path / "v.mp4"
    path.write_bytes(b"a" * (12 * MB))
    plan = plan_chunks(path.stat().st_size, 5 * MB)
    seen = []
    with respx.mock, httpx.Client() as http:
        route = respx.put("https://upload.example/x").mock(return_value=httpx.Response(206))
        TikTokClient(settings, http, "tok").upload_file(
            "https://upload.example/x", path, plan, progress=seen.append
        )
    ranges = [c.request.headers["Content-Range"] for c in route.calls]
    assert ranges == [f"bytes 0-{5 * MB - 1}/{12 * MB}", f"bytes {5 * MB}-{12 * MB - 1}/{12 * MB}"]
    assert seen == [5 * MB, 12 * MB]
    assert route.calls[1].request.headers["Content-Type"] == "video/mp4"


# --- Tokens erneuern / trennen ---------------------------------------------------------
def test_expiring_token_is_refreshed(settings, monkeypatch, db_session):
    configure(settings, monkeypatch)
    acc = account(db_session, expires_in_hours=0)
    with respx.mock, httpx.Client() as http:
        route = respx.post(f"{API}/v2/oauth/token/").mock(
            return_value=httpx.Response(
                200, json=token_response(access_token="act.neu", refresh_token="rft.neu")
            )
        )
        assert tiktok_accounts.access_token_for(db_session, settings, http, acc) == "act.neu"
    body = parse_qs(route.calls[0].request.content.decode())
    assert body["grant_type"] == ["refresh_token"] and body["refresh_token"] == ["rft.example-refresh"]
    token = tiktok_accounts.active_token(acc)
    assert token.refresh_token == "rft.neu" and token.expires_at > utcnow() + timedelta(hours=23)


def test_rejected_refresh_marks_account_for_reconnect(settings, monkeypatch, db_session):
    configure(settings, monkeypatch)
    acc = account(db_session, expires_in_hours=0)
    with respx.mock, httpx.Client() as http:
        respx.post(f"{API}/v2/oauth/token/").mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant", "error_description": "expired"})
        )
        with pytest.raises(TikTokAuthError):
            tiktok_accounts.access_token_for(db_session, settings, http, acc)
    assert tiktok_accounts.active_token(acc) is None
    assert db_session.scalar(select(ActivityLog).where(ActivityLog.message.like("%neu verbunden%")))


def test_periodic_refresh_only_touches_expiring_tokens(settings, monkeypatch, db_session):
    configure(settings, monkeypatch)
    account(db_session, expires_in_hours=20)
    with respx.mock, httpx.Client() as http:
        route = respx.post(f"{API}/v2/oauth/token/")
        assert tiktok_accounts.refresh_expiring_tokens(db_session, settings, http) == {
            "refreshed": 0,
            "failed": 0,
        }
        assert not route.called


def test_disconnect_revokes_token(admin_client, settings, monkeypatch, db_session):
    configure(settings, monkeypatch)
    acc = account(db_session)
    db_session.commit()
    with respx.mock:
        revoke = respx.post(f"{API}/v2/oauth/revoke/").mock(return_value=httpx.Response(200, json={}))
        r = post_form(admin_client, f"/tiktok/{acc.id}/disconnect", csrf_page="/tiktok")
    assert r.status_code == 303 and revoke.called
    assert parse_qs(revoke.calls[0].request.content.decode())["token"] == ["act.example-access"]
    db_session.expire_all()
    acc = db_session.get(TikTokAccount, acc.id)
    assert not acc.is_active and tiktok_accounts.active_token(acc) is None
    assert db_session.scalar(select(OAuthToken)).revoked_at is not None


def test_refresh_creator_info_route(admin_client, settings, monkeypatch, db_session):
    configure(settings, monkeypatch)
    acc = account(db_session)
    db_session.commit()
    changed = dict(CREATOR_INFO, privacy_level_options=["SELF_ONLY"])
    with respx.mock:
        respx.post(f"{API}/v2/post/publish/creator_info/query/").mock(return_value=envelope(changed))
        post_form(admin_client, f"/tiktok/{acc.id}/refresh", csrf_page="/tiktok")
    db_session.expire_all()
    assert db_session.get(TikTokAccount, acc.id).creator_info["privacy_level_options"] == ["SELF_ONLY"]
