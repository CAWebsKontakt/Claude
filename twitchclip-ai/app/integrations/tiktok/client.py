"""Client für die TikTok Content Posting API (FILE_UPLOAD).

Ablauf: Creator-Info abfragen → initialisieren (Direct Post oder Inbox) → Datei in Stücken per PUT an
die upload_url senden → Status abfragen. Regeln für die Stückelung (Media Transfer Guide):
- Videos unter 5 MB: genau ein Stück (chunk_size = video_size)
- sonst chunk_size zwischen 5 und 64 MB, total_chunk_count = floor(video_size / chunk_size),
  der Rest wird dem letzten Stück zugeschlagen (letztes Stück darf größer sein)
- höchstens 1000 Stücke, Upload strikt nacheinander
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.integrations.tiktok.errors import TikTokAPIError, TikTokAuthError, TikTokRateLimitedError

logger = logging.getLogger(__name__)

MB = 1024 * 1024
MIN_CHUNK = 5 * MB
MAX_CHUNK = 64 * MB
MAX_CHUNKS = 1000

PRIVACY_LABELS = {
    "PUBLIC_TO_EVERYONE": "Öffentlich",
    "MUTUAL_FOLLOW_FRIENDS": "Freunde (gegenseitig folgend)",
    "FOLLOWER_OF_CREATOR": "Follower",
    "SELF_ONLY": "Nur ich (privat)",
}


@dataclass(frozen=True)
class CreatorInfo:
    avatar_url: str | None
    username: str | None
    nickname: str | None
    privacy_level_options: list[str]
    comment_disabled: bool
    duet_disabled: bool
    stitch_disabled: bool
    max_video_post_duration_sec: int

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> CreatorInfo:
        return cls(
            avatar_url=data.get("creator_avatar_url"),
            username=data.get("creator_username"),
            nickname=data.get("creator_nickname"),
            privacy_level_options=list(data.get("privacy_level_options") or []),
            comment_disabled=bool(data.get("comment_disabled")),
            duet_disabled=bool(data.get("duet_disabled")),
            stitch_disabled=bool(data.get("stitch_disabled")),
            max_video_post_duration_sec=int(data.get("max_video_post_duration_sec") or 0),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "creator_avatar_url": self.avatar_url,
            "creator_username": self.username,
            "creator_nickname": self.nickname,
            "privacy_level_options": self.privacy_level_options,
            "comment_disabled": self.comment_disabled,
            "duet_disabled": self.duet_disabled,
            "stitch_disabled": self.stitch_disabled,
            "max_video_post_duration_sec": self.max_video_post_duration_sec,
        }


@dataclass(frozen=True)
class ChunkPlan:
    video_size: int
    chunk_size: int
    total_chunk_count: int

    def ranges(self) -> list[tuple[int, int]]:
        """(erstes Byte, letztes Byte) je Stück; das letzte Stück enthält den Rest."""
        out = []
        for i in range(self.total_chunk_count):
            first = i * self.chunk_size
            last = self.video_size - 1 if i == self.total_chunk_count - 1 else first + self.chunk_size - 1
            out.append((first, last))
        return out

    def source_info(self) -> dict[str, Any]:
        return {
            "source": "FILE_UPLOAD",
            "video_size": self.video_size,
            "chunk_size": self.chunk_size,
            "total_chunk_count": self.total_chunk_count,
        }


def plan_chunks(video_size: int, preferred_chunk: int = 10 * MB) -> ChunkPlan:
    if video_size <= 0:
        raise ValueError("Leere Datei")
    if video_size < MIN_CHUNK:
        return ChunkPlan(video_size, video_size, 1)
    chunk = min(max(preferred_chunk, MIN_CHUNK), MAX_CHUNK, video_size)
    count = video_size // chunk
    while count > MAX_CHUNKS:
        chunk = min(MAX_CHUNK, chunk * 2)
        count = video_size // chunk
    return ChunkPlan(video_size, chunk, max(1, count))


@dataclass
class PublishInit:
    publish_id: str
    upload_url: str | None


@dataclass
class PublishStatus:
    status: str
    fail_reason: str | None = None
    post_ids: list[str] = field(default_factory=list)
    uploaded_bytes: int | None = None


class TikTokClient:
    def __init__(self, settings: Settings, http: httpx.Client, access_token: str) -> None:
        self.settings = settings
        self.http = http
        self.access_token = access_token

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.http.post(
                f"{self.settings.tiktok_api_base}{path}",
                json=body,
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise TikTokAPIError("network", exc.__class__.__name__, retryable=True) from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error") or {}
        code = error.get("code") if isinstance(error, dict) else str(error)
        log_id = error.get("log_id") if isinstance(error, dict) else None
        if response.status_code == 429 or code == "rate_limit_exceeded":
            raise TikTokRateLimitedError(float(response.headers.get("retry-after") or 60), log_id=log_id)
        if response.status_code == 401 or code in ("access_token_invalid", "token_expired"):
            raise TikTokAuthError(f"TikTok lehnt das Zugriffstoken ab ({code or response.status_code})")
        if response.status_code >= 500:
            raise TikTokAPIError(
                code or "server_error",
                error.get("message") if isinstance(error, dict) else None,
                status_code=response.status_code,
                log_id=log_id,
                retryable=True,
            )
        if code not in (None, "", "ok") or response.status_code >= 400:
            raise TikTokAPIError(
                code or "http_error",
                error.get("message") if isinstance(error, dict) else None,
                status_code=response.status_code,
                log_id=log_id,
            )
        return payload.get("data") or {}

    def creator_info(self) -> CreatorInfo:
        return CreatorInfo.from_api(self._post("/v2/post/publish/creator_info/query/", {}))

    def init_direct_post(self, post_info: dict[str, Any], plan: ChunkPlan) -> PublishInit:
        data = self._post(
            "/v2/post/publish/video/init/", {"post_info": post_info, "source_info": plan.source_info()}
        )
        return PublishInit(publish_id=str(data["publish_id"]), upload_url=data.get("upload_url"))

    def init_inbox_upload(self, plan: ChunkPlan) -> PublishInit:
        data = self._post("/v2/post/publish/inbox/video/init/", {"source_info": plan.source_info()})
        return PublishInit(publish_id=str(data["publish_id"]), upload_url=data.get("upload_url"))

    def fetch_status(self, publish_id: str) -> PublishStatus:
        data = self._post("/v2/post/publish/status/fetch/", {"publish_id": publish_id})
        ids = data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or []
        return PublishStatus(
            status=str(data.get("status", "")),
            fail_reason=data.get("fail_reason") or None,
            post_ids=[str(i) for i in ids],
            uploaded_bytes=data.get("uploaded_bytes"),
        )

    def upload_file(
        self, upload_url: str, path: Path, plan: ChunkPlan, *, progress: Callable[[int], None] | None = None
    ) -> None:
        """PUT der Stücke nacheinander mit Content-Range. Erwartet 206 (Teil) bzw. 201 (fertig)."""
        with path.open("rb") as handle:
            for first, last in plan.ranges():
                handle.seek(first)
                chunk = handle.read(last - first + 1)
                try:
                    response = self.http.put(
                        upload_url,
                        content=chunk,
                        headers={
                            "Content-Type": "video/mp4",
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {first}-{last}/{plan.video_size}",
                        },
                        timeout=httpx.Timeout(60.0, write=300.0),
                    )
                except httpx.HTTPError as exc:
                    raise TikTokAPIError("upload_network", exc.__class__.__name__, retryable=True) from exc
                if response.status_code not in (200, 201, 206):
                    raise TikTokAPIError(
                        "upload_failed",
                        f"HTTP {response.status_code} bei Bytes {first}-{last}",
                        status_code=response.status_code,
                    )
                if progress:
                    progress(last + 1)
