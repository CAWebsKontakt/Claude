"""Gemeinsame Helfer für TikTok-Tests: Konfiguration, Konto, Mock-Antworten der Content Posting API."""

from __future__ import annotations

from datetime import timedelta

import httpx
from pydantic import SecretStr

from app.models import OAuthToken, TikTokAccount
from app.timeutil import utcnow
from tests import factories

API = "https://open.tiktokapis.com"
UPLOAD_URL = "https://open-upload.tiktokapis.com/video/?upload_id=67890&upload_token=Xza123"

CREATOR_INFO = {
    "creator_avatar_url": "https://p16.tiktokcdn.com/avatar.jpeg",
    "creator_username": "partner_clips",
    "creator_nickname": "Partner Clips",
    "privacy_level_options": ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
    "comment_disabled": False,
    "duet_disabled": True,
    "stitch_disabled": False,
    "max_video_post_duration_sec": 300,
}


def configure(settings, monkeypatch, *, direct: bool = True) -> None:
    monkeypatch.setattr(settings, "tiktok_client_key", "tt-client-key")
    monkeypatch.setattr(settings, "tiktok_client_secret", SecretStr("tt-client-secret"))
    monkeypatch.setattr(settings, "tiktok_direct_post_enabled", direct)


def envelope(
    data: dict | None = None, code: str = "ok", message: str = "", status: int = 200
) -> httpx.Response:
    return httpx.Response(
        status, json={"data": data or {}, "error": {"code": code, "message": message, "log_id": "log-1"}}
    )


def token_response(**overrides) -> dict:
    body = {
        "open_id": "open-123",
        "access_token": "act.example-access",
        "refresh_token": "rft.example-refresh",
        "expires_in": 86400,
        "refresh_expires_in": 31536000,
        "scope": "user.info.basic,video.upload,video.publish",
        "token_type": "Bearer",
    }
    body.update(overrides)
    return body


def account(db, *, scopes=("user.info.basic", "video.upload", "video.publish"), expires_in_hours: int = 20):
    obj = TikTokAccount(
        open_id="open-123",
        display_name="Partner Clips",
        username="partner_clips",
        creator_info=dict(CREATOR_INFO),
        creator_info_fetched_at=utcnow(),
    )
    db.add(obj)
    db.flush()
    db.add(
        OAuthToken(
            provider="tiktok",
            tiktok_account_id=obj.id,
            external_user_id="open-123",
            scopes=list(scopes),
            access_token="act.example-access",
            refresh_token="rft.example-refresh",
            expires_at=utcnow() + timedelta(hours=expires_in_hours),
        )
    )
    db.flush()
    db.refresh(obj)
    return obj


def rendered_setup(db, settings, *, publishing: bool = True, duration: float = 30.0):
    """Autorisierter Streamer mit fertig geschnittenem Highlight und Video-Datei."""
    s = factories.authorized_streamer(db, settings, publishing=publishing)
    h = factories.highlight(db, s, score=80, status="rendered", analysis={"audio_spikes": [4.0]})
    path = settings.renders_dir / f"{h.id}.mp4"
    video = factories.rendered_video(db, h, path)
    video.duration = duration
    path.with_suffix(".srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHallo\n", encoding="utf-8")
    db.flush()
    db.refresh(h)
    return s, h, video
