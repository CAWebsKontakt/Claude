"""Beispielantworten der Twitch-API (Struktur gemäß offizieller Helix-/EventSub-Dokumentation)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.integrations.twitch.eventsub import compute_signature

API = "https://api.twitch.tv/helix"
AUTH = "https://id.twitch.tv/oauth2"
EVENTSUB_SECRET = "test-eventsub-secret-0123456789"
CALLBACK = "https://clips.example.com/webhooks/twitch/eventsub"


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def token_response(token: str = "app-token-1", expires_in: int = 5011271) -> dict:
    return {"access_token": token, "expires_in": expires_in, "token_type": "bearer"}


def user(uid: str, login: str, display: str | None = None) -> dict:
    return {
        "id": uid,
        "login": login,
        "display_name": display or login.capitalize(),
        "type": "",
        "broadcaster_type": "partner",
        "description": "",
        "profile_image_url": f"https://static-cdn.jtvnw.net/jtv_user_pictures/{login}-profile_image-300x300.png",
        "offline_image_url": "",
        "view_count": 0,
        "created_at": "2016-12-14T20:32:28Z",
    }


def stream(
    uid: str, login: str, stream_id: str, *, title="Ranked Grind", game="Valorant", viewers=1234, started=None
) -> dict:
    started = started or datetime.now(UTC) - timedelta(minutes=30)
    return {
        "id": stream_id,
        "user_id": uid,
        "user_login": login,
        "user_name": login.capitalize(),
        "game_id": "516575",
        "game_name": game,
        "type": "live",
        "title": title,
        "tags": ["Deutsch"],
        "viewer_count": viewers,
        "started_at": iso(started),
        "language": "de",
        "thumbnail_url": "https://static-cdn.jtvnw.net/previews-ttv/live_user_x-{width}x{height}.jpg",
        "is_mature": False,
    }


def clip(
    clip_id: str,
    broadcaster_id: str,
    *,
    views: int = 100,
    created: datetime | None = None,
    duration: float = 30.0,
    video_id: str | None = "2000000001",
    vod_offset: int | None = 3600,
    featured: bool = False,
    title: str = "Unglaublicher Clutch",
) -> dict:
    created = created or datetime.now(UTC) - timedelta(hours=2)
    return {
        "id": clip_id,
        "url": f"https://clips.twitch.tv/{clip_id}",
        "embed_url": f"https://clips.twitch.tv/embed?clip={clip_id}",
        "broadcaster_id": broadcaster_id,
        "broadcaster_name": "Kanal",
        "creator_id": "999",
        "creator_name": "zuschauer",
        "video_id": video_id or "",
        "game_id": "516575",
        "language": "de",
        "title": title,
        "view_count": views,
        "created_at": iso(created),
        "thumbnail_url": f"https://static-cdn.jtvnw.net/twitch-clips-thumbnails-prod/{clip_id}/preview-480x272.jpg",
        "duration": duration,
        "vod_offset": vod_offset,
        "is_featured": featured,
    }


def video(video_id: str, uid: str, stream_id: str) -> dict:
    return {
        "id": video_id,
        "stream_id": stream_id,
        "user_id": uid,
        "user_login": "kanal",
        "user_name": "Kanal",
        "title": "Stream",
        "description": "",
        "created_at": "2026-11-17T18:00:00Z",
        "published_at": "2026-11-17T18:00:00Z",
        "url": f"https://www.twitch.tv/videos/{video_id}",
        "thumbnail_url": "",
        "viewable": "public",
        "view_count": 10,
        "language": "de",
        "type": "archive",
        "duration": "3h8m33s",
        "muted_segments": None,
    }


def subscription(
    sub_id: str, sub_type: str, broadcaster_id: str, *, status="enabled", callback=CALLBACK, cost=1
) -> dict:
    return {
        "id": sub_id,
        "status": status,
        "type": sub_type,
        "version": "1",
        "cost": cost,
        "condition": {"broadcaster_user_id": broadcaster_id},
        "transport": {"method": "webhook", "callback": callback},
        "created_at": "2026-11-17T10:11:12.634234626Z",
    }


def eventsub_headers(
    body: bytes,
    message_type: str,
    *,
    message_id: str = "msg-1",
    timestamp: datetime | None = None,
    secret: str = EVENTSUB_SECRET,
) -> dict[str, str]:
    ts = (timestamp or datetime.now(UTC)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    ) + "123Z"  # Nanosekunden wie bei Twitch
    return {
        "Twitch-Eventsub-Message-Id": message_id,
        "Twitch-Eventsub-Message-Retry": "0",
        "Twitch-Eventsub-Message-Type": message_type,
        "Twitch-Eventsub-Message-Signature": compute_signature(secret, message_id, ts, body),
        "Twitch-Eventsub-Message-Timestamp": ts,
        "Twitch-Eventsub-Subscription-Type": "stream.online",
        "Twitch-Eventsub-Subscription-Version": "1",
        "Content-Type": "application/json",
    }


def online_notification(broadcaster_id: str, login: str, stream_id: str = "9001") -> bytes:
    return json.dumps(
        {
            "subscription": subscription("sub-on", "stream.online", broadcaster_id),
            "event": {
                "id": stream_id,
                "broadcaster_user_id": broadcaster_id,
                "broadcaster_user_login": login,
                "broadcaster_user_name": login.capitalize(),
                "type": "live",
                "started_at": iso(datetime.now(UTC) - timedelta(seconds=20)),
            },
        }
    ).encode()


def offline_notification(broadcaster_id: str, login: str) -> bytes:
    return json.dumps(
        {
            "subscription": subscription("sub-off", "stream.offline", broadcaster_id),
            "event": {
                "broadcaster_user_id": broadcaster_id,
                "broadcaster_user_login": login,
                "broadcaster_user_name": login.capitalize(),
            },
        }
    ).encode()


def user_token_response(
    access="user-access-new", refresh="user-refresh-new", scopes=None, expires_in=14400
) -> dict:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": expires_in,
        "scope": scopes or ["channel:manage:clips", "editor:manage:clips"],
        "token_type": "bearer",
    }


def validate_response(user_id="111", login="partner", scopes=None, client_id="test-client-id") -> dict:
    return {
        "client_id": client_id,
        "login": login,
        "scopes": scopes or ["channel:manage:clips", "editor:manage:clips"],
        "user_id": user_id,
        "expires_in": 14000,
    }
