"""Berechtigte Clip-Verarbeitung (Modus B): Kandidaten planen, Clips aus dem VOD erzeugen, herunterladen.

Jeder Schritt prüft die Rechte unmittelbar vor der Ausführung erneut (ensure_can_process). Fehlen sie,
wird der Auftrag übersprungen – unabhängig davon, was bereits in der Warteschlange steht.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import (
    ActivityCategory,
    ActivityLevel,
    ClipRequestSource,
    ClipRequestStatus,
    ClipStatus,
)
from app.integrations.twitch.client import HelixClient
from app.models import ClipRequest, Streamer, StreamSession, TwitchClip, User
from app.services.activity import log_activity
from app.services.media_probe import VIDEO_SUFFIXES, probe_media, sha256_file
from app.services.storage import is_within
from app.services.streamers import RightsError, ensure_can_process
from app.services.twitch_authorization import call_with_user_token

logger = logging.getLogger(__name__)

# Twitch: Clips aus VODs sind 5–60 Sekunden lang
MIN_CLIP_SECONDS = 5
MAX_CLIP_SECONDS = 60
# Zuschauer-Clips, deren Start weniger als 45 s auseinanderliegt, gelten als derselbe Moment
MIN_CANDIDATE_DISTANCE = 45
DEMO_LOGIN = "demo_testmodus"


class ClipRequestError(ValueError):
    """Ungültige Anforderung (Meldung für das Dashboard)."""


class ClipNotReadyError(Exception):
    """Twitch hat den Clip noch nicht fertig verarbeitet – später erneut versuchen."""


@dataclass
class DownloadResult:
    path: Path
    size: int
    sha256: str
    duplicate_of: int | None = None


def _skip(db: Session, request: ClipRequest, reason: str) -> None:
    request.status = ClipRequestStatus.SKIPPED.value
    request.error = reason[:2000]
    log_activity(
        db,
        ActivityLevel.WARNING,
        ActivityCategory.PIPELINE,
        f"Clip-Auftrag #{request.id} übersprungen: {reason}",
        streamer_id=request.streamer_id,
    )


# ---------------------------------------------------------------------------
# Planung
# ---------------------------------------------------------------------------
def plan_viewer_clips(db: Session, settings: Settings, session: StreamSession) -> list[ClipRequest]:
    """Wählt die besten, sich nicht überlappenden Zuschauer-Clips eines beendeten Streams aus."""
    streamer = db.get(Streamer, session.streamer_id)
    if streamer is None or not session.vod_id:
        return []
    ensure_can_process(streamer)
    clips = db.scalars(
        select(TwitchClip)
        .where(
            TwitchClip.streamer_id == streamer.id,
            TwitchClip.video_id == session.vod_id,
            TwitchClip.vod_offset.is_not(None),
            TwitchClip.status != ClipStatus.DISMISSED.value,
        )
        .order_by(TwitchClip.discovery_score.desc(), TwitchClip.view_count.desc())
    ).all()
    selected: list[TwitchClip] = []
    for clip in clips:
        if any(abs(clip.vod_offset - other.vod_offset) < MIN_CANDIDATE_DISTANCE for other in selected):
            continue
        selected.append(clip)
        if len(selected) >= settings.clip_candidates_per_stream:
            break

    created: list[ClipRequest] = []
    for clip in selected:
        key = f"viewer:{clip.twitch_clip_id}"
        if db.scalar(select(ClipRequest.id).where(ClipRequest.dedup_key == key)) is not None:
            continue
        duration = max(1, round(clip.duration))
        request = ClipRequest(
            streamer_id=streamer.id,
            stream_session_id=session.id,
            source=ClipRequestSource.VIEWER_CLIP.value,
            dedup_key=key,
            vod_id=session.vod_id,
            vod_offset=int(clip.vod_offset) + duration,  # Ende (Twitch-Konvention bei Create Clip From VOD)
            duration=duration,
            twitch_clip_id=clip.twitch_clip_id,
            title=clip.title[:140],
            status=ClipRequestStatus.PLANNED.value,
            reason={"discovery_score": clip.discovery_score, "view_count": clip.view_count},
        )
        db.add(request)
        created.append(request)
    db.flush()
    if created:
        log_activity(
            db,
            ActivityLevel.INFO,
            ActivityCategory.PIPELINE,
            f"{len(created)} Clip-Kandidaten für den Stream vom {session.started_at:%d.%m.%Y} geplant",
            streamer_id=streamer.id,
        )
    return created


def plan_chat_spike_clips(db: Session, settings: Settings, session: StreamSession) -> list[ClipRequest]:
    """Zusätzliche Kandidaten an Chat-Spitzen, die nicht schon durch Zuschauer-Clips abgedeckt sind."""
    from app.services import chat_stats

    streamer = db.get(Streamer, session.streamer_id)
    if streamer is None or not session.vod_id or not settings.chat_stats_enabled:
        return []
    existing = db.scalars(select(ClipRequest).where(ClipRequest.stream_session_id == session.id)).all()
    budget = settings.clip_candidates_per_stream - len(existing)
    if budget <= 0:
        return []
    buckets = chat_stats.session_buckets(db, session)
    spikes = chat_stats.find_spikes(buckets, session, settings.chat_spike_factor, limit=budget)
    created: list[ClipRequest] = []
    duration = settings.clip_duration_max
    for spike in spikes:
        # Chat reagiert verzögert: Ausschnitt endet kurz nach der Spitze
        start = max(0, int(spike) - duration + 8)
        end = start + duration
        if any(
            r.vod_offset and abs((r.vod_offset - (r.duration or 0) / 2) - (start + duration / 2)) < 45
            for r in existing
        ):
            continue
        try:
            created.append(
                request_vod_clip(
                    db,
                    streamer,
                    session,
                    start_seconds=start,
                    duration=duration,
                    actor=None,
                    source=ClipRequestSource.VOD_WINDOW,
                    title=f"{streamer.name} – Chat-Highlight",
                    reason={"chat_spike_vod_seconds": spike},
                )  # fmt: skip
            )
        except ClipRequestError:
            continue
        if end and len(created) >= budget:
            break
    return created


def format_timecode(seconds: int) -> str:
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def parse_timecode(value: str) -> int:
    """'1:02:03', '62:03' oder '3723' → Sekunden."""
    text = (value or "").strip()
    if not text:
        raise ClipRequestError("Bitte einen Zeitpunkt angeben (z. B. 1:02:03).")
    parts = text.split(":")
    if len(parts) > 3 or not all(p.strip().isdigit() for p in parts):
        raise ClipRequestError(f"Ungültiger Zeitpunkt: {value}")
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def request_vod_clip(
    db: Session,
    streamer: Streamer,
    session: StreamSession,
    *,
    start_seconds: int,
    duration: int,
    actor: User | None,
    source: ClipRequestSource = ClipRequestSource.MANUAL,
    title: str | None = None,
    reason: dict | None = None,
) -> ClipRequest:
    try:
        ensure_can_process(streamer)
    except RightsError as exc:
        raise ClipRequestError(str(exc)) from exc
    if session.streamer_id != streamer.id:
        raise ClipRequestError("Stream gehört nicht zu diesem Kanal.")
    if not session.vod_id:
        raise ClipRequestError("Für diesen Stream ist kein VOD bekannt.")
    if not MIN_CLIP_SECONDS <= duration <= MAX_CLIP_SECONDS:
        raise ClipRequestError(
            f"Die Länge muss zwischen {MIN_CLIP_SECONDS} und {MAX_CLIP_SECONDS} Sekunden liegen."
        )
    if start_seconds < 0:
        raise ClipRequestError("Der Zeitpunkt darf nicht negativ sein.")
    end = start_seconds + duration
    key = f"vod:{session.vod_id}:{end}:{duration}"
    if db.scalar(select(ClipRequest.id).where(ClipRequest.dedup_key == key)) is not None:
        raise ClipRequestError("Dieser Ausschnitt wurde bereits angefordert.")
    request = ClipRequest(
        streamer_id=streamer.id,
        stream_session_id=session.id,
        source=source.value,
        dedup_key=key,
        vod_id=session.vod_id,
        vod_offset=end,
        duration=duration,
        title=(title or f"{streamer.name} – Highlight")[:140],
        status=ClipRequestStatus.PLANNED.value,
        reason=reason,
        requested_by_user_id=actor.id if actor else None,
    )
    db.add(request)
    db.flush()
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.PIPELINE,
        f"Clip aus VOD angefordert: {format_timecode(start_seconds)}, {duration} s (Auftrag #{request.id})",
        user_id=actor.id if actor else None,
        streamer_id=streamer.id,
    )
    return request


# ---------------------------------------------------------------------------
# Ausführung
# ---------------------------------------------------------------------------
def create_clip(db: Session, settings: Settings, client: HelixClient, request: ClipRequest) -> bool:
    """Erzeugt einen Clip aus dem VOD. False = übersprungen (Rechte fehlen)."""
    streamer = db.get(Streamer, request.streamer_id)
    try:
        rights = ensure_can_process(streamer)
    except RightsError as exc:
        _skip(db, request, str(exc))
        return False
    if request.twitch_clip_id:
        return True  # bereits erzeugt (Wiederholung des Jobs)
    request.status = ClipRequestStatus.CREATING.value
    request.attempts += 1
    request.oauth_token_id = rights.token.id
    created = call_with_user_token(
        db,
        settings,
        client.http,
        rights.token,
        lambda credential: client.create_clip_from_vod(
            credential,
            broadcaster_id=streamer.twitch_user_id,
            vod_id=request.vod_id,
            vod_offset=request.vod_offset,
            duration=request.duration,
            title=request.title,
        ),
    )
    request.twitch_clip_id = created.id
    request.status = ClipRequestStatus.WAITING.value
    request.error = None
    db.flush()
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.PIPELINE,
        f"Clip {created.id} aus VOD {request.vod_id} erzeugt (Auftrag #{request.id})",
        streamer_id=streamer.id,
    )
    return True


def _download_file(
    http: httpx.Client, url: str, target: Path, tmp_dir: Path, max_bytes: int
) -> tuple[int, str]:
    if not url.startswith("https://"):
        raise ValueError("Download-Adresse ist nicht HTTPS")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"download-{uuid.uuid4().hex}.part"
    try:
        size = _stream_to_file(http, url, tmp_path, max_bytes)
        if size == 0:
            raise ClipNotReadyError("Leere Datei erhalten")
        digest = sha256_file(tmp_path)
        os.replace(tmp_path, target)
        return size, digest
    finally:
        tmp_path.unlink(missing_ok=True)  # Teil-Downloads nie liegen lassen


def _stream_to_file(http: httpx.Client, url: str, path: Path, max_bytes: int) -> int:
    size = 0
    try:
        # Bewusst ohne Twitch-Header: die Adresse zeigt auf das CDN und ist bereits signiert.
        with http.stream(
            "GET", url, timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True
        ) as response:
            if response.status_code != 200:
                raise ClipNotReadyError(f"Download-Server antwortet mit HTTP {response.status_code}")
            content_type = response.headers.get("content-type", "")
            if content_type and not content_type.startswith(
                ("video/", "application/octet-stream", "binary/")
            ):
                raise ValueError(f"Unerwarteter Inhaltstyp: {content_type}")
            with path.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(f"Datei größer als {max_bytes // 1024**2} MB – abgebrochen")
                    handle.write(chunk)
    except httpx.TransportError as exc:
        # Netzwerkfehler zum CDN sind vorübergehend → später erneut versuchen
        raise ClipNotReadyError(f"Download-Server nicht erreichbar ({exc.__class__.__name__})") from exc
    return size


def download_clip(
    db: Session, settings: Settings, client: HelixClient, request: ClipRequest
) -> DownloadResult | None:
    """Lädt einen berechtigten Clip über die offizielle Download-Adresse. None = übersprungen."""
    streamer = db.get(Streamer, request.streamer_id)
    try:
        rights = ensure_can_process(streamer)
    except RightsError as exc:
        _skip(db, request, str(exc))
        return None
    if not request.twitch_clip_id:
        raise ClipRequestError("Auftrag hat noch keine Clip-ID")
    request.status = ClipRequestStatus.DOWNLOADING.value
    request.attempts += 1
    request.oauth_token_id = rights.token.id
    urls = call_with_user_token(
        db,
        settings,
        client.http,
        rights.token,
        lambda credential: client.get_clip_download_urls(
            credential, broadcaster_id=streamer.twitch_user_id, clip_ids=[request.twitch_clip_id]
        ),
    )
    download = urls.get(request.twitch_clip_id)
    if download is None:
        raise ClipNotReadyError("Twitch liefert noch keine Download-Adresse")

    target = settings.downloads_dir / str(streamer.id) / f"clip-{request.id}.mp4"
    size, digest = _download_file(
        client.http, download.landscape_url, target, settings.tmp_dir, settings.clip_download_max_mb * 1024**2
    )
    return _finalize_download(db, request, target, size, digest)


def _finalize_download(
    db: Session, request: ClipRequest, path: Path, size: int, digest: str
) -> DownloadResult:
    duplicate = db.scalar(
        select(ClipRequest).where(
            ClipRequest.sha256 == digest,
            ClipRequest.id != request.id,
            ClipRequest.status == ClipRequestStatus.DOWNLOADED.value,
        )
    )
    if duplicate is not None:
        if path != Path(duplicate.file_path or ""):
            path.unlink(missing_ok=True)
        request.status = ClipRequestStatus.DUPLICATE.value
        request.sha256 = digest
        request.error = f"Identisch mit Auftrag #{duplicate.id}"
        db.flush()
        return DownloadResult(
            path=Path(duplicate.file_path or ""), size=size, sha256=digest, duplicate_of=duplicate.id
        )
    request.file_path = str(path)
    request.file_size = size
    request.sha256 = digest
    request.media_info = probe_media(path)
    request.status = ClipRequestStatus.DOWNLOADED.value
    request.error = None
    db.flush()
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.PIPELINE,
        f"Clip-Auftrag #{request.id} heruntergeladen ({size / 1024**2:.1f} MB) – bereit für die KI-Analyse",
        streamer_id=request.streamer_id,
    )
    return DownloadResult(path=path, size=size, sha256=digest)


RETRYABLE_STATUSES = (ClipRequestStatus.SKIPPED, ClipRequestStatus.FAILED)


def reset_for_retry(db: Session, request: ClipRequest, *, actor: User) -> str:
    """Übersprungenen/fehlgeschlagenen Auftrag erneut starten (z. B. nach neuer Autorisierung).

    Gibt "create" oder "download" zurück – je nachdem, welcher Schritt als Nächstes ansteht.
    """
    analysis_retry = (
        request.status == ClipRequestStatus.DOWNLOADED
        and request.analyzed_at is None
        and (request.error or "").startswith("Analyse:")
    )
    if request.status not in RETRYABLE_STATUSES and not analysis_retry:
        raise ClipRequestError("Nur übersprungene oder fehlgeschlagene Aufträge können wiederholt werden.")
    if request.source == ClipRequestSource.SAMPLE and not analysis_retry:
        raise ClipRequestError("Beispieldateien müssen nicht erneut geladen werden.")
    streamer = db.get(Streamer, request.streamer_id)
    try:
        ensure_can_process(streamer)
    except RightsError as exc:
        raise ClipRequestError(str(exc)) from exc
    request.attempts = 0
    request.error = None
    if analysis_retry:
        if not request.file_path or not Path(request.file_path).is_file():
            raise ClipRequestError("Die Quelldatei ist nicht mehr vorhanden.")
        db.flush()
        log_activity(
            db,
            ActivityLevel.INFO,
            ActivityCategory.PIPELINE,
            f"Analyse von Clip-Auftrag #{request.id} erneut gestartet",
            user_id=actor.id,
            streamer_id=request.streamer_id,
        )
        return "analyze"
    step = "download" if request.twitch_clip_id else "create"
    request.status = (ClipRequestStatus.WAITING if step == "download" else ClipRequestStatus.PLANNED).value
    db.flush()
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.PIPELINE,
        f"Clip-Auftrag #{request.id} erneut gestartet",
        user_id=actor.id,
        streamer_id=request.streamer_id,
    )
    return step


def mark_failed(db: Session, request: ClipRequest, error: str) -> None:
    request.status = ClipRequestStatus.FAILED.value
    request.error = error[:2000]
    log_activity(
        db,
        ActivityLevel.ERROR,
        ActivityCategory.PIPELINE,
        f"Clip-Auftrag #{request.id} fehlgeschlagen: {error}",
        streamer_id=request.streamer_id,
    )


# ---------------------------------------------------------------------------
# Testmodus: Beispieldateien
# ---------------------------------------------------------------------------
def ensure_demo_streamer(db: Session) -> Streamer:
    streamer = db.scalar(select(Streamer).where(Streamer.is_demo.is_(True)))
    if streamer is None:
        streamer = Streamer(
            twitch_login=DEMO_LOGIN,
            display_name="Testmodus (Beispieldateien)",
            mode="authorized",
            is_demo=True,
            notes="Automatisch angelegt für TEST_MODE. Verarbeitet nur Dateien aus /data/samples.",
        )
        db.add(streamer)
        db.flush()
        log_activity(
            db,
            ActivityLevel.INFO,
            ActivityCategory.SYSTEM,
            "Demo-Kanal für den Testmodus angelegt",
            streamer_id=streamer.id,
        )
    return streamer


def ingest_samples(db: Session, settings: Settings) -> list[ClipRequest]:
    """Übernimmt neue Videos aus /data/samples als verarbeitbare Aufträge des Demo-Kanals."""
    if not settings.test_mode:
        return []
    settings.samples_dir.mkdir(parents=True, exist_ok=True)
    streamer = ensure_demo_streamer(db)
    created: list[ClipRequest] = []
    for path in sorted(settings.samples_dir.iterdir()):
        if (
            not path.is_file()
            or path.suffix.lower() not in VIDEO_SUFFIXES
            or not is_within(path, settings.samples_dir)
        ):
            continue
        digest = sha256_file(path)
        key = f"sample:{digest}"
        if db.scalar(select(ClipRequest.id).where(ClipRequest.dedup_key == key)) is not None:
            continue
        info = probe_media(path)
        if info is None and settings.app_env != "test":
            logger.warning("Beispieldatei übersprungen (kein lesbares Video)", extra={"path": str(path)})
            continue
        request = ClipRequest(
            streamer_id=streamer.id,
            source=ClipRequestSource.SAMPLE.value,
            dedup_key=key,
            duration=round(info["duration"]) if info else None,
            title=path.stem[:140],
            status=ClipRequestStatus.DOWNLOADED.value,
            file_path=str(path),
            file_size=path.stat().st_size,
            sha256=digest,
            media_info=info,
            reason={"sample": path.name},
        )
        db.add(request)
        created.append(request)
    db.flush()
    if created:
        log_activity(
            db,
            ActivityLevel.INFO,
            ActivityCategory.PIPELINE,
            f"Testmodus: {len(created)} Beispieldatei(en) übernommen",
            streamer_id=streamer.id,
        )
    return created
