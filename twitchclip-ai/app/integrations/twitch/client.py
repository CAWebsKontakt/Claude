"""Schlanker, synchroner Client für die offizielle Twitch Helix API.

Eigenschaften:
- App-Access-Token wird automatisch bezogen, zwischengespeichert und bei 401 einmalig erneuert.
- Rate-Limits werden aus den Antwort-Headern gelernt und prozessübergreifend beachtet.
- Netzwerkfehler und 5xx werden mit kurzem Backoff wiederholt; danach entscheidet RQ über weitere Versuche.
- Batch-Abfragen (bis zu 100 IDs) und Cursor-Paginierung.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from redis import Redis

from app import __version__
from app.config import Settings, get_settings
from app.integrations.twitch.auth import AppTokenProvider
from app.integrations.twitch.errors import (
    EventSubConflictError,
    TwitchAPIError,
    TwitchAuthError,
    TwitchRateLimitedError,
)
from app.integrations.twitch.ratelimit import MAX_INLINE_WAIT_SECONDS, RateLimiter
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

BATCH_SIZE = 100
_FRACTION_RE = re.compile(r"\.(\d+)")
_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def parse_twitch_time(value: str | None) -> datetime | None:
    """RFC3339 mit beliebig vielen Nachkommastellen (Twitch liefert teils Nanosekunden)."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    text = _FRACTION_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_twitch_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_video_duration(value: str | None) -> int:
    """'3h2m1s' -> 10921 Sekunden."""
    if not value:
        return 0
    match = _DURATION_RE.match(value.strip())
    if not match:
        return 0
    hours, minutes, seconds = (int(x) if x else 0 for x in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _chunks(items: list[str], size: int = BATCH_SIZE) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ---------------------------------------------------------------------------
# Datenobjekte
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TwitchUser:
    id: str
    login: str
    display_name: str
    profile_image_url: str | None
    created_at: datetime | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> TwitchUser:
        return cls(
            id=str(data["id"]),
            login=str(data["login"]).lower(),
            display_name=data.get("display_name") or data["login"],
            profile_image_url=data.get("profile_image_url") or None,
            created_at=parse_twitch_time(data.get("created_at")),
        )


@dataclass(frozen=True)
class TwitchStream:
    id: str
    user_id: str
    user_login: str
    title: str
    game_name: str
    viewer_count: int
    started_at: datetime | None
    language: str | None
    user_name: str = ""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> TwitchStream:
        return cls(
            id=str(data["id"]),
            user_id=str(data["user_id"]),
            user_login=str(data.get("user_login", "")).lower(),
            title=data.get("title") or "",
            game_name=data.get("game_name") or "",
            viewer_count=int(data.get("viewer_count") or 0),
            started_at=parse_twitch_time(data.get("started_at")),
            language=data.get("language") or None,
            user_name=data.get("user_name") or "",
        )


@dataclass(frozen=True)
class TwitchClipData:
    id: str
    url: str
    embed_url: str | None
    broadcaster_id: str
    creator_name: str | None
    video_id: str | None
    game_id: str | None
    language: str | None
    title: str
    view_count: int
    created_at: datetime
    thumbnail_url: str | None
    duration: float
    vod_offset: int | None
    is_featured: bool

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> TwitchClipData:
        created = parse_twitch_time(data.get("created_at"))
        if created is None:
            raise ValueError("Clip ohne created_at")
        vod_offset = data.get("vod_offset")
        return cls(
            id=str(data["id"]),
            url=data.get("url") or f"https://clips.twitch.tv/{data['id']}",
            embed_url=data.get("embed_url") or None,
            broadcaster_id=str(data.get("broadcaster_id", "")),
            creator_name=data.get("creator_name") or None,
            video_id=data.get("video_id") or None,
            game_id=data.get("game_id") or None,
            language=data.get("language") or None,
            title=data.get("title") or "",
            view_count=int(data.get("view_count") or 0),
            created_at=created,
            thumbnail_url=data.get("thumbnail_url") or None,
            duration=float(data.get("duration") or 0.0),
            vod_offset=int(vod_offset) if vod_offset is not None else None,
            is_featured=bool(data.get("is_featured", False)),
        )


@dataclass(frozen=True)
class TwitchVideo:
    id: str
    user_id: str
    stream_id: str | None
    title: str
    created_at: datetime | None
    duration_seconds: int
    url: str
    type: str

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> TwitchVideo:
        return cls(
            id=str(data["id"]),
            user_id=str(data.get("user_id", "")),
            stream_id=str(data["stream_id"]) if data.get("stream_id") else None,
            title=data.get("title") or "",
            created_at=parse_twitch_time(data.get("created_at")),
            duration_seconds=parse_video_duration(data.get("duration")),
            url=data.get("url") or "",
            type=data.get("type") or "",
        )


@dataclass(frozen=True)
class EventSubInfo:
    id: str
    type: str
    version: str
    status: str
    cost: int
    broadcaster_user_id: str | None
    callback: str | None
    client_id: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> EventSubInfo:
        condition = data.get("condition") or {}
        transport = data.get("transport") or {}
        return cls(
            id=str(data["id"]),
            type=str(data.get("type", "")),
            version=str(data.get("version", "1")),
            status=str(data.get("status", "")),
            cost=int(data.get("cost") or 0),
            broadcaster_user_id=condition.get("broadcaster_user_id"),
            callback=transport.get("callback"),
            client_id=condition.get("client_id"),
        )


@dataclass(frozen=True)
class UserCredential:
    """Nutzer-Token eines Streamers/Editors für einen einzelnen Aufruf (nie loggen!)."""

    user_id: str
    access_token: str = field(repr=False)


@dataclass(frozen=True)
class CreatedClip:
    id: str
    edit_url: str | None


@dataclass(frozen=True)
class ClipDownload:
    clip_id: str
    landscape_url: str
    portrait_url: str | None


@dataclass
class EventSubListing:
    subscriptions: list[EventSubInfo] = field(default_factory=list)
    total: int = 0
    total_cost: int = 0
    max_total_cost: int = 0


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class HelixClient:
    def __init__(
        self,
        settings: Settings | None = None,
        redis: Redis | None = None,
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 2,
    ) -> None:
        self.settings = settings or get_settings()
        self.redis = redis or get_redis()
        self._own_http = http is None
        self.http = http or httpx.Client(
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"User-Agent": f"TwitchClipAI/{__version__}"},
        )
        self.tokens = AppTokenProvider(self.settings, self.redis, self.http)
        self.limiter = RateLimiter(self.redis, "app", sleep=sleep)
        self.sleep = sleep
        self.max_retries = max_retries

    # Kontextmanager -------------------------------------------------------
    def __enter__(self) -> HelixClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._own_http:
            self.http.close()

    # Kern -----------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str]] | dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        expected: Iterable[int] = (200,),
        user_token: UserCredential | None = None,
    ) -> httpx.Response:
        """Führt eine Helix-Anfrage aus – mit App-Token oder, falls angegeben, mit einem Nutzer-Token.

        Bei Nutzer-Tokens führt 401 zu TwitchAuthError; die Erneuerung übernimmt der Aufrufer
        (siehe app/services/twitch_authorization.py), weil sie die Datenbank betrifft.
        """
        expected = tuple(expected)
        url = f"{self.settings.twitch_api_base}{path}"
        limiter = self.limiter if user_token is None else self._user_limiter(user_token.user_id)
        attempt = 0
        token_refreshed = False
        while True:
            limiter.before_request()
            bearer = user_token.access_token if user_token is not None else self.tokens.get_token()
            headers = {
                "Client-Id": self.settings.twitch_client_id,
                "Authorization": f"Bearer {bearer}",
            }
            try:
                response = self.http.request(method, url, params=params, json=json, headers=headers)
            except httpx.TransportError as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    self.sleep(2**attempt)
                    continue
                raise TwitchAPIError(
                    f"Twitch nicht erreichbar ({exc.__class__.__name__})", retryable=True
                ) from exc

            limiter.update_from_headers(response.headers)
            status = response.status_code

            if status == 401:
                if user_token is not None:
                    raise TwitchAuthError("Twitch lehnt das Nutzer-Token ab (401)")
                if not token_refreshed:
                    token_refreshed = True
                    self.tokens.invalidate()
                    continue
                raise TwitchAuthError("Twitch lehnt das App-Token ab (401)")
            if status == 429:
                wait = limiter.seconds_until_reset(response.headers)
                if wait <= MAX_INLINE_WAIT_SECONDS and attempt < self.max_retries:
                    attempt += 1
                    if "ratelimit-reset" not in response.headers:
                        self.sleep(wait)
                    # Sonst wartet before_request() anhand der soeben gespeicherten Header.
                    continue
                raise TwitchRateLimitedError(wait)
            if status >= 500:
                if attempt < self.max_retries:
                    attempt += 1
                    self.sleep(2**attempt)
                    continue
                raise TwitchAPIError(f"Twitch-Serverfehler {status}", status_code=status, retryable=True)
            if status == 409:
                raise EventSubConflictError("Abo existiert bereits", status_code=409)
            if status not in expected:
                raise TwitchAPIError(
                    f"Twitch {method} {path}: HTTP {status} – {self._error_message(response)}",
                    status_code=status,
                )
            return response

    def _user_limiter(self, user_id: str) -> RateLimiter:
        return RateLimiter(self.redis, f"user:{user_id}", sleep=self.sleep)

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
            return str(payload.get("message") or payload.get("error") or payload)[:300]
        except ValueError:
            return response.text[:300]

    def _paginate(
        self, path: str, params: list[tuple[str, str]], *, max_pages: int
    ) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        for _page in range(max_pages):
            page_params = list(params)
            if cursor:
                page_params.append(("after", cursor))
            payload = self.request("GET", path, params=page_params).json()
            yield payload
            cursor = (payload.get("pagination") or {}).get("cursor")
            if not cursor or not payload.get("data"):
                return

    # Endpunkte --------------------------------------------------------------
    def get_users(self, *, logins: Iterable[str] = (), ids: Iterable[str] = ()) -> list[TwitchUser]:
        keys = [("login", login.lower()) for login in logins] + [("id", str(i)) for i in ids]
        users: list[TwitchUser] = []
        for start in range(0, len(keys), BATCH_SIZE):
            payload = self.request("GET", "/users", params=keys[start : start + BATCH_SIZE]).json()
            users.extend(TwitchUser.from_api(item) for item in payload.get("data", []))
        return users

    def get_channel_languages(self, broadcaster_ids: Iterable[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for chunk in _chunks(list(broadcaster_ids)):
            payload = self.request("GET", "/channels", params=[("broadcaster_id", i) for i in chunk]).json()
            for item in payload.get("data", []):
                if item.get("broadcaster_language"):
                    result[str(item["broadcaster_id"])] = item["broadcaster_language"]
        return result

    def get_streams(self, user_ids: Iterable[str]) -> list[TwitchStream]:
        streams: list[TwitchStream] = []
        for chunk in _chunks(list(user_ids)):
            params = [("user_id", i) for i in chunk] + [("type", "live"), ("first", "100")]
            payload = self.request("GET", "/streams", params=params).json()
            streams.extend(TwitchStream.from_api(item) for item in payload.get("data", []))
        return streams

    def get_top_streams(
        self, *, language: str | None = None, game_ids: Iterable[str] = (), max_pages: int = 3
    ) -> list[TwitchStream]:
        """Live-Streams nach Zuschauerzahl absteigend (öffentlich, App-Token) – Grundlage der Hype-Liste."""
        params: list[tuple[str, str]] = [("type", "live"), ("first", "100")]
        if language:
            params.append(("language", language))
        params += [("game_id", str(g)) for g in list(game_ids)[:100]]
        streams: list[TwitchStream] = []
        for payload in self._paginate("/streams", params, max_pages=max_pages):
            streams.extend(TwitchStream.from_api(item) for item in payload.get("data", []))
        return streams

    def get_clips(
        self,
        broadcaster_id: str,
        started_at: datetime,
        ended_at: datetime,
        *,
        max_pages: int = 5,
    ) -> list[TwitchClipData]:
        params = [
            ("broadcaster_id", broadcaster_id),
            ("started_at", format_twitch_time(started_at)),
            ("ended_at", format_twitch_time(ended_at)),
            ("first", "100"),
        ]
        clips: list[TwitchClipData] = []
        for payload in self._paginate("/clips", params, max_pages=max_pages):
            for item in payload.get("data", []):
                try:
                    clips.append(TwitchClipData.from_api(item))
                except (KeyError, ValueError):
                    logger.warning(
                        "Unvollständiger Clip-Datensatz übersprungen", extra={"clip": item.get("id")}
                    )
        return clips

    def get_videos(self, user_id: str, *, video_type: str = "archive", first: int = 20) -> list[TwitchVideo]:
        params = [("user_id", user_id), ("type", video_type), ("first", str(min(first, 100)))]
        payload = self.request("GET", "/videos", params=params).json()
        return [TwitchVideo.from_api(item) for item in payload.get("data", [])]

    # Autorisierte Clip-Endpunkte (Modus B) --------------------------------
    def create_clip_from_vod(
        self,
        credential: UserCredential,
        *,
        broadcaster_id: str,
        vod_id: str,
        vod_offset: int,
        duration: int,
        title: str | None = None,
    ) -> CreatedClip:
        """Erzeugt einen Clip aus einem VOD. vod_offset ist das ENDE des Clips (Sekunden)."""
        params = [
            ("editor_id", credential.user_id),
            ("broadcaster_id", broadcaster_id),
            ("vod_id", vod_id),
            ("vod_offset", str(vod_offset)),
            ("duration", str(duration)),
        ]
        if title:
            params.append(("title", title[:140]))
        payload = self.request(
            "POST",
            self.settings.twitch_vod_clip_path,
            params=params,
            expected=(200, 202),
            user_token=credential,
        ).json()
        data = (payload.get("data") or [{}])[0]
        if not data.get("id"):
            raise TwitchAPIError("Antwort von Create Clip From VOD enthält keine Clip-ID")
        return CreatedClip(id=str(data["id"]), edit_url=data.get("edit_url"))

    def get_clip_download_urls(
        self, credential: UserCredential, *, broadcaster_id: str, clip_ids: Iterable[str]
    ) -> dict[str, ClipDownload]:
        """Temporäre Download-Adressen (max. 10 Clips je Anfrage)."""
        result: dict[str, ClipDownload] = {}
        ids = list(clip_ids)
        for start in range(0, len(ids), 10):
            params = [("editor_id", credential.user_id), ("broadcaster_id", broadcaster_id)] + [
                ("clip_id", clip_id) for clip_id in ids[start : start + 10]
            ]
            payload = self.request(
                "GET", self.settings.twitch_clip_download_path, params=params, user_token=credential
            ).json()
            for item in payload.get("data", []):
                clip_id = str(item.get("clip_id") or item.get("id") or "")
                landscape = item.get("landscape_download_url") or item.get("download_url")
                if clip_id and landscape:
                    result[clip_id] = ClipDownload(
                        clip_id=clip_id,
                        landscape_url=landscape,
                        portrait_url=item.get("portrait_download_url"),
                    )
        return result

    # EventSub --------------------------------------------------------------
    def list_eventsub(self, *, max_pages: int = 50) -> EventSubListing:
        listing = EventSubListing()
        for payload in self._paginate("/eventsub/subscriptions", [], max_pages=max_pages):
            listing.subscriptions.extend(EventSubInfo.from_api(item) for item in payload.get("data", []))
            listing.total = int(payload.get("total") or 0)
            listing.total_cost = int(payload.get("total_cost") or 0)
            listing.max_total_cost = int(payload.get("max_total_cost") or 0)
        return listing

    def create_eventsub(
        self,
        sub_type: str,
        broadcaster_user_id: str | None,
        callback: str,
        secret: str,
        *,
        version: str = "1",
        condition: dict[str, str] | None = None,
    ) -> tuple[EventSubInfo, dict[str, int]]:
        body = {
            "type": sub_type,
            "version": version,
            "condition": condition or {"broadcaster_user_id": broadcaster_user_id},
            "transport": {"method": "webhook", "callback": callback, "secret": secret},
        }
        payload = self.request("POST", "/eventsub/subscriptions", json=body, expected=(202,)).json()
        info = EventSubInfo.from_api(payload["data"][0])
        totals = {
            "total": int(payload.get("total") or 0),
            "total_cost": int(payload.get("total_cost") or 0),
            "max_total_cost": int(payload.get("max_total_cost") or 0),
        }
        return info, totals

    def delete_eventsub(self, subscription_id: str) -> None:
        try:
            self.request("DELETE", "/eventsub/subscriptions", params={"id": subscription_id}, expected=(204,))
        except TwitchAPIError as exc:
            if exc.status_code == 404:
                return  # bereits gelöscht
            raise


def twitch_client(settings: Settings | None = None, redis: Redis | None = None) -> HelixClient:
    return HelixClient(settings, redis)
