import time
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.integrations.twitch.auth import CACHE_KEY
from app.integrations.twitch.client import parse_twitch_time, parse_video_duration
from app.integrations.twitch.errors import (
    EventSubConflictError,
    TwitchAPIError,
    TwitchAuthError,
    TwitchNotConfiguredError,
    TwitchRateLimitedError,
)
from tests.twitch_payloads import API, AUTH, clip, subscription, token_response, user


def test_time_and_duration_parsing():
    assert parse_twitch_time("2019-11-16T10:11:12.634234626Z") == datetime(
        2019, 11, 16, 10, 11, 12, 634234, tzinfo=UTC
    )
    assert parse_twitch_time("2020-10-11T10:11:12Z") == datetime(2020, 10, 11, 10, 11, 12, tzinfo=UTC)
    assert parse_twitch_time(None) is None
    assert parse_video_duration("3h8m33s") == 11313
    assert parse_video_duration("45s") == 45
    assert parse_video_duration("kaputt") == 0


def test_not_configured(settings, redis):
    from app.integrations.twitch.client import HelixClient

    with pytest.raises(TwitchNotConfiguredError):
        HelixClient(settings, redis)


@respx.mock
def test_app_token_is_cached_encrypted_and_sent(helix, redis):
    token_route = respx.post(f"{AUTH}/token").mock(
        return_value=httpx.Response(200, json=token_response("tok-123"))
    )
    users_route = respx.get(f"{API}/users").mock(
        return_value=httpx.Response(200, json={"data": [user("1", "kanal")]})
    )
    helix.get_users(logins=["Kanal"])
    helix.get_users(logins=["kanal"])
    assert token_route.call_count == 1
    request = users_route.calls.last.request
    assert request.headers["Authorization"] == "Bearer tok-123"
    assert request.headers["Client-Id"] == "test-client-id"
    assert request.url.params.get_list("login") == ["kanal"]
    assert b"tok-123" not in redis.get(CACHE_KEY)  # verschlüsselt in Redis


@respx.mock
def test_token_endpoint_error(helix):
    respx.post(f"{AUTH}/token").mock(
        return_value=httpx.Response(400, json={"message": "invalid client secret"})
    )
    with pytest.raises(TwitchAuthError, match="invalid client secret"):
        helix.get_users(logins=["kanal"])


@respx.mock
def test_401_refreshes_token_once(helix):
    respx.post(f"{AUTH}/token").mock(
        side_effect=[
            httpx.Response(200, json=token_response("alt")),
            httpx.Response(200, json=token_response("neu")),
        ]
    )
    route = respx.get(f"{API}/users").mock(
        side_effect=[
            httpx.Response(401, json={"message": "Invalid OAuth token"}),
            httpx.Response(200, json={"data": []}),
        ]
    )
    assert helix.get_users(logins=["kanal"]) == []
    assert route.calls[1].request.headers["Authorization"] == "Bearer neu"


@respx.mock
def test_429_waits_until_reset_then_retries(helix):
    respx.post(f"{AUTH}/token").mock(return_value=httpx.Response(200, json=token_response()))
    reset = int(time.time()) + 3
    respx.get(f"{API}/users").mock(
        side_effect=[
            httpx.Response(
                429,
                headers={"Ratelimit-Limit": "800", "Ratelimit-Remaining": "0", "Ratelimit-Reset": str(reset)},
            ),
            httpx.Response(200, json={"data": [user("1", "kanal")]}),
        ]
    )
    assert len(helix.get_users(logins=["kanal"])) == 1
    assert len(helix.sleeps) == 1 and 1 <= helix.sleeps[0] <= 5


@respx.mock
def test_429_with_long_reset_raises_for_rq_retry(helix):
    respx.post(f"{AUTH}/token").mock(return_value=httpx.Response(200, json=token_response()))
    respx.get(f"{API}/users").mock(
        return_value=httpx.Response(
            429, headers={"Ratelimit-Remaining": "0", "Ratelimit-Reset": str(int(time.time()) + 90)}
        )
    )
    with pytest.raises(TwitchRateLimitedError) as info:
        helix.get_users(logins=["kanal"])
    assert info.value.retry_after > 60


@respx.mock
def test_low_remaining_quota_is_respected_before_request(helix):
    respx.post(f"{AUTH}/token").mock(return_value=httpx.Response(200, json=token_response()))
    route = respx.get(f"{API}/users").mock(
        return_value=httpx.Response(
            200,
            json={"data": []},
            headers={
                "Ratelimit-Limit": "800",
                "Ratelimit-Remaining": "3",
                "Ratelimit-Reset": str(int(time.time()) + 50),
            },
        )
    )
    helix.get_users(logins=["a1c"])
    with pytest.raises(TwitchRateLimitedError):
        helix.get_users(logins=["b2c"])  # Kontingent fast leer, Reset zu weit weg -> zurück an RQ
    assert route.call_count == 1
    assert helix.limiter.state()["remaining"] == 3


@respx.mock
def test_server_errors_are_retried(helix):
    respx.post(f"{AUTH}/token").mock(return_value=httpx.Response(200, json=token_response()))
    respx.get(f"{API}/streams").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"data": []})]
    )
    assert helix.get_streams(["1"]) == []
    assert helix.sleeps == [2]


@respx.mock
def test_persistent_server_error_is_retryable(helix):
    respx.post(f"{AUTH}/token").mock(return_value=httpx.Response(200, json=token_response()))
    respx.get(f"{API}/streams").mock(return_value=httpx.Response(500))
    with pytest.raises(TwitchAPIError) as info:
        helix.get_streams(["1"])
    assert info.value.retryable and info.value.status_code == 500


@respx.mock
def test_users_are_batched_by_100(helix):
    respx.post(f"{AUTH}/token").mock(return_value=httpx.Response(200, json=token_response()))
    route = respx.get(f"{API}/users").mock(return_value=httpx.Response(200, json={"data": []}))
    helix.get_users(logins=[f"kanal{i:03d}" for i in range(150)])
    assert route.call_count == 2
    assert len(route.calls[0].request.url.params.get_list("login")) == 100
    assert len(route.calls[1].request.url.params.get_list("login")) == 50


@respx.mock
def test_clips_pagination_and_time_format(helix):
    respx.post(f"{AUTH}/token").mock(return_value=httpx.Response(200, json=token_response()))
    route = respx.get(f"{API}/clips").mock(
        side_effect=[
            httpx.Response(
                200, json={"data": [clip("A", "1"), clip("B", "1")], "pagination": {"cursor": "c1"}}
            ),
            httpx.Response(
                200, json={"data": [clip("C", "1", vod_offset=None, video_id=None)], "pagination": {}}
            ),
        ]
    )
    start = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)
    end = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    clips = helix.get_clips("1", start, end)
    assert [c.id for c in clips] == ["A", "B", "C"]
    assert clips[2].vod_offset is None and clips[2].video_id is None
    params = route.calls[0].request.url.params
    assert params["started_at"] == "2026-11-16T00:00:00Z" and params["ended_at"] == "2026-11-16T12:00:00Z"
    assert route.calls[1].request.url.params["after"] == "c1"


@respx.mock
def test_eventsub_create_conflict_and_delete(helix):
    respx.post(f"{AUTH}/token").mock(return_value=httpx.Response(200, json=token_response()))
    respx.post(f"{API}/eventsub/subscriptions").mock(
        side_effect=[
            httpx.Response(
                202,
                json={
                    "data": [
                        subscription(
                            "s1", "stream.online", "1", status="webhook_callback_verification_pending"
                        )
                    ],
                    "total": 1,
                    "total_cost": 1,
                    "max_total_cost": 10000,
                },
            ),
            httpx.Response(409, json={"message": "subscription already exists"}),
        ]
    )
    respx.delete(f"{API}/eventsub/subscriptions").mock(side_effect=[httpx.Response(204), httpx.Response(404)])
    info, totals = helix.create_eventsub("stream.online", "1", "https://x.example/cb", "secret-1234567")
    assert info.status == "webhook_callback_verification_pending" and totals["max_total_cost"] == 10000
    body = respx.calls[-1].request.content
    assert b'"method": "webhook"' in body or b'"method":"webhook"' in body
    with pytest.raises(EventSubConflictError):
        helix.create_eventsub("stream.online", "1", "https://x.example/cb", "secret-1234567")
    helix.delete_eventsub("s1")
    helix.delete_eventsub("s1")  # 404 = bereits gelöscht, kein Fehler


@respx.mock
def test_client_error_message_is_reported(helix):
    respx.post(f"{AUTH}/token").mock(return_value=httpx.Response(200, json=token_response()))
    respx.get(f"{API}/clips").mock(
        return_value=httpx.Response(400, json={"message": "Invalid broadcaster_id"})
    )
    with pytest.raises(TwitchAPIError, match="Invalid broadcaster_id") as info:
        helix.get_clips("x", datetime.now(UTC), datetime.now(UTC))
    assert not info.value.retryable
