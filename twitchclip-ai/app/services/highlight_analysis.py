"""KI-Highlight-Erkennung je berechtigtem Clip und Auswahl der besten 3–5 je Stream.

Ablauf je Clip-Auftrag:
1. Rechte erneut prüfen (ensure_can_process)
2. Ton extrahieren → Lautheitsverlauf und Ausbrüche
3. Bild analysieren (OpenCV) → Bewegung, Schnitte, Facecam, Bild-Hashes
4. Sprache erkennen (faster-whisper, optional) → Ausrufe, Schlüsselwörter, Lachen, Wort-Zeitstempel
5. Chat-Reaktion im Zeitfenster (falls Chat-Statistik erlaubt)
6. Besten Ausschnitt (CLIP_DURATION_MIN–MAX) wählen, Bewertung 0–100
7. Duplikate erkennen (gleicher VOD-Bereich oder ähnliche Bild-Hashes)
8. Auswahl: je Stream die besten HIGHLIGHTS_PER_STREAM_MAX über MIN_HIGHLIGHT_SCORE, Tageslimit beachten
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import scoring
from app.ai.audio import AudioError, AudioProfile, analyze_audio_file, extract_audio
from app.ai.transcribe import Transcriber, Transcript, speech_excitement
from app.ai.vision import VisionError, VisionProfile, analyze_video, hamming
from app.config import Settings
from app.enums import (
    ActivityCategory,
    ActivityLevel,
    ClipRequestSource,
    HighlightStatus,
    SourceType,
)
from app.models import ClipRequest, Highlight, Streamer, StreamSession, TwitchClip
from app.services import chat_stats
from app.services.activity import log_activity
from app.services.streamers import ensure_can_process
from app.timeutil import local_day_bounds, utcnow

logger = logging.getLogger(__name__)

SELECTION_PREFIX = "Auswahl: "
ACTIVE_HIGHLIGHT_STATUSES = (
    HighlightStatus.CANDIDATE.value,
    HighlightStatus.RENDERING.value,
    HighlightStatus.RENDERED.value,
    HighlightStatus.QUEUED.value,
    HighlightStatus.PUBLISHED.value,
)


class AnalysisError(RuntimeError):
    pass


@dataclass
class ClipAnalysis:
    duration: float
    audio: AudioProfile | None
    vision: VisionProfile | None
    transcript: Transcript | None


def selection_group(request: ClipRequest, settings: Settings) -> str:
    if request.stream_session_id:
        return f"session-{request.stream_session_id}"
    day = utcnow().astimezone(settings.tz).date().isoformat()
    return f"samples-{request.streamer_id}-{day}"


def analyze_media(path: Path, settings: Settings, transcriber: Transcriber | None) -> ClipAnalysis:
    tmp = settings.tmp_dir / f"analysis-{uuid.uuid4().hex}.wav"
    audio = None
    transcript = None
    try:
        try:
            extract_audio(path, tmp)
            audio = analyze_audio_file(tmp)
        except AudioError as exc:
            logger.warning("Tonanalyse fehlgeschlagen", extra={"error": str(exc)})
        try:
            vision = analyze_video(path)
        except VisionError as exc:
            raise AnalysisError(str(exc)) from exc
        if transcriber is not None and audio is not None and audio.has_audio:
            language = None if settings.clip_language == "auto" else settings.clip_language
            try:
                transcript = transcriber.transcribe(tmp, language)
            except Exception as exc:  # noqa: BLE001 – ohne Transkript weiter
                logger.warning("Spracherkennung fehlgeschlagen", extra={"error": str(exc)[:300]})
    finally:
        tmp.unlink(missing_ok=True)
    duration = vision.duration or (len(audio.excitement) if audio else 0.0)
    return ClipAnalysis(duration=duration, audio=audio, vision=vision, transcript=transcript)


def _viewer_component(db: Session, request: ClipRequest) -> float | None:
    if request.source != ClipRequestSource.VIEWER_CLIP or not request.twitch_clip_id:
        return None
    clip = db.scalar(select(TwitchClip).where(TwitchClip.twitch_clip_id == request.twitch_clip_id))
    return round(clip.discovery_score / 100, 3) if clip else None


def _find_duplicate(
    db: Session,
    settings: Settings,
    request: ClipRequest,
    hashes: list[str],
    window: scoring.Window,
    audio_fp: list[float],
) -> Highlight | None:
    since = utcnow() - timedelta(days=14)
    others = db.scalars(
        select(Highlight).where(
            Highlight.streamer_id == request.streamer_id,
            Highlight.created_at >= since,
            Highlight.status != HighlightStatus.DUPLICATE.value,
        )
    ).all()
    start_abs = (request.start_seconds or 0) + window.start
    end_abs = (request.start_seconds or 0) + window.end
    for other in others:
        other_request = db.get(ClipRequest, other.clip_request_id) if other.clip_request_id else None
        # 1) Derselbe VOD-Bereich (Überlappung > 50 %)
        if request.vod_id and other_request is not None and other_request.vod_id == request.vod_id:
            o_start = (other_request.start_seconds or 0) + other.start_seconds
            o_end = (other_request.start_seconds or 0) + other.end_seconds
            overlap = min(end_abs, o_end) - max(start_abs, o_start)
            if overlap > 0.5 * min(end_abs - start_abs, o_end - o_start):
                return other
        # 2) Bild UND Ton ähnlich (z. B. dieselbe Szene erneut verarbeitet oder neu kodiert)
        if hashes and other.perceptual_hash:
            other_hashes = other.perceptual_hash.split(",")
            pairs = [hamming(a, b) for a, b in zip(hashes, other_hashes, strict=False)]
            if pairs and max(pairs) <= settings.duplicate_hash_distance:
                other_fp = (other.analysis or {}).get("audio_fp")
                if not audio_fp or not other_fp or _correlation(audio_fp, other_fp) >= 0.85:
                    return other
    return None


def _correlation(a: list[float], b: list[float]) -> float:
    length = min(len(a), len(b))
    if length < 3:
        return 0.0
    x = np.array(a[:length], dtype=float)
    y = np.array(b[:length], dtype=float)
    if x.std() < 1e-6 or y.std() < 1e-6:
        return 1.0 if np.allclose(x, y, atol=1.0) else 0.0
    return float(np.corrcoef(x, y)[0, 1])


def _audio_fingerprint(audio: AudioProfile | None, window: scoring.Window) -> list[float]:
    """Lautheit je Sekunde im gewählten Fenster (dB) – Ton-„Fingerabdruck“ für die Duplikatprüfung."""
    if audio is None:
        return []
    per = max(1, round(1 / audio.window_seconds))
    seconds = [max(audio.db[i : i + per]) for i in range(0, len(audio.db), per)]
    return [round(v, 1) for v in seconds[int(window.start) : int(window.end)]]


def analyze_clip_request(
    db: Session, settings: Settings, request: ClipRequest, transcriber: Transcriber | None
) -> Highlight:
    streamer = db.get(Streamer, request.streamer_id)
    ensure_can_process(streamer)
    if not request.file_path or not Path(request.file_path).is_file():
        raise AnalysisError("Quelldatei nicht (mehr) vorhanden")

    result = analyze_media(Path(request.file_path), settings, transcriber)
    seconds = max(1, int(result.duration + 0.999))
    audio_series = (result.audio.excitement if result.audio else [])[:seconds]
    speech_series = speech_excitement(result.transcript, result.duration)
    motion_series = (result.vision.motion if result.vision else [])[:seconds]
    cut_series = [0.0] * seconds
    for cut in result.vision.scene_cuts if result.vision else []:
        cut_series[min(seconds - 1, int(cut))] = 1.0
    signal = scoring.combine_series((audio_series, 0.45), (speech_series, 0.35), (motion_series, 0.2))
    window = scoring.best_window(
        signal, result.duration, settings.clip_duration_min, settings.clip_duration_max
    )

    session = db.get(StreamSession, request.stream_session_id) if request.stream_session_id else None
    chat = None
    if session is not None and request.start_seconds is not None:
        buckets = chat_stats.session_buckets(db, session)
        chat = chat_stats.window_signal(
            buckets, session, request.start_seconds + window.start, request.start_seconds + window.end
        )
    components = {
        "ton": scoring.window_component(audio_series, window.start, window.end) if result.audio else None,
        "sprache": scoring.window_component(speech_series, window.start, window.end)
        if result.transcript
        else None,
        "chat": scoring.chat_component(chat.ratio if chat else None),
        "bild": round(
            0.7 * scoring.window_component(motion_series, window.start, window.end)
            + 0.3 * scoring.window_component(cut_series, window.start, window.end),
            3,
        )
        if result.vision
        else None,
        "zuschauer": _viewer_component(db, request),
        "laenge": scoring.duration_fit(
            window.duration, settings.clip_duration_min, settings.clip_duration_max
        ),
    }
    score, breakdown = scoring.total_score(components)
    if chat:
        breakdown["Chat: Spitze/Minute"] = chat.peak_per_minute

    words = result.transcript.between(window.start, window.end) if result.transcript else []
    hashes = result.vision.frame_hashes if result.vision else []
    content_hash = hashlib.sha256(
        f"{request.sha256}:{window.start:.1f}:{window.end:.1f}".encode()
    ).hexdigest()
    existing = db.scalar(select(Highlight).where(Highlight.content_hash == content_hash))
    if existing is not None:
        request.analyzed_at = utcnow()
        return existing

    audio_fp = _audio_fingerprint(result.audio, window)
    duplicate = _find_duplicate(db, settings, request, hashes, window, audio_fp)
    source_type = {
        ClipRequestSource.SAMPLE: SourceType.SAMPLE,
        ClipRequestSource.VIEWER_CLIP: SourceType.CLIP,
    }.get(ClipRequestSource(request.source), SourceType.VOD)
    highlight = Highlight(
        streamer_id=request.streamer_id,
        stream_session_id=request.stream_session_id,
        clip_request_id=request.id,
        source_type=source_type.value,
        source_ref=request.twitch_clip_id or request.dedup_key,
        twitch_clip_id=request.twitch_clip_id,
        source_file_path=request.file_path,
        start_seconds=window.start,
        end_seconds=window.end,
        score=score,
        score_breakdown=breakdown,
        transcript=" ".join(w.text.strip() for w in words).strip() or None,
        language=result.transcript.language if result.transcript else None,
        title=request.title,
        hashtags=[],
        content_hash=content_hash,
        perceptual_hash=",".join(hashes) or None,
        selection_group=selection_group(request, settings),
        status=(HighlightStatus.DUPLICATE if duplicate else HighlightStatus.CANDIDATE).value,
        error=f"Duplikat von Highlight #{duplicate.id}" if duplicate else None,
        analysis={
            "duration": result.duration,
            "facecam": result.vision.facecam if result.vision else None,
            "video_size": [result.vision.width, result.vision.height] if result.vision else None,
            "words": [
                {
                    "start": round(w.start - window.start, 2),
                    "end": round(w.end - window.start, 2),
                    "text": w.text.strip(),
                }
                for w in words
            ],
            "audio_spikes": [
                round(t - window.start, 1)
                for t in (result.audio.spikes if result.audio else [])
                if window.start <= t <= window.end
            ],  # fmt: skip
            "scene_cuts": [
                round(t - window.start, 1)
                for t in (result.vision.scene_cuts if result.vision else [])
                if window.start <= t <= window.end
            ],  # fmt: skip
            "loudness_median_db": result.audio.median_db if result.audio else None,
            "audio_fp": audio_fp,
            "transcribed": result.transcript is not None,
        },
    )
    db.add(highlight)
    request.analyzed_at = utcnow()
    db.flush()
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.PIPELINE,
        f"Highlight #{highlight.id} analysiert: Bewertung {score:.0f}/100"
        + (f" – Duplikat von #{duplicate.id}" if duplicate else ""),
        streamer_id=request.streamer_id,
    )
    return highlight


def group_is_complete(db: Session, group: str) -> bool:
    """Alle heruntergeladenen Clips der Gruppe sind analysiert (sonst erst später auswählen)."""
    if not group.startswith("session-"):
        return True
    session_id = int(group.split("-", 1)[1])
    pending = db.scalar(
        select(func.count(ClipRequest.id)).where(
            ClipRequest.stream_session_id == session_id,
            ClipRequest.status.in_(["planned", "creating", "waiting", "downloading", "downloaded"]),
            ClipRequest.analyzed_at.is_(None),
        )
    )
    return not pending


def select_group(db: Session, settings: Settings, group: str) -> list[Highlight]:
    """Wählt je Stream (bzw. Testmodus-Tag) die besten Highlights aus. Gibt die ausgewählten zurück."""
    rows = db.scalars(
        select(Highlight).where(
            Highlight.selection_group == group,
            Highlight.status.in_([HighlightStatus.CANDIDATE.value, HighlightStatus.REJECTED.value]),
        )
    ).all()
    # Manuell verworfene Highlights (ohne Auswahl-Vermerk) bleiben verworfen
    pool = [
        h
        for h in rows
        if h.status == HighlightStatus.CANDIDATE or (h.error or "").startswith(SELECTION_PREFIX)
    ]
    pool.sort(key=lambda h: h.score, reverse=True)

    day_start, day_end = local_day_bounds(settings.tz)
    used_today = (
        db.scalar(
            select(func.count(Highlight.id)).where(
                Highlight.created_at >= day_start,
                Highlight.created_at < day_end,
                Highlight.status.in_(ACTIVE_HIGHLIGHT_STATUSES),
                Highlight.selection_group != group,
            )
        )
        or 0
    )
    remaining_today = max(0, settings.max_clips_per_day - used_today)
    limit = min(settings.highlights_per_stream_max, remaining_today)

    selected: list[Highlight] = []
    for highlight in pool:
        if highlight.score < settings.min_highlight_score:
            reason = (
                f"Bewertung {highlight.score:.0f} unter MIN_HIGHLIGHT_SCORE {settings.min_highlight_score}"
            )
        elif len(selected) >= limit:
            reason = (
                f"Tageslimit MAX_CLIPS_PER_DAY ({settings.max_clips_per_day}) erreicht"
                if limit < settings.highlights_per_stream_max and len(selected) >= remaining_today
                else f"nicht unter den besten {settings.highlights_per_stream_max} dieses Streams"
            )
        else:
            highlight.status = HighlightStatus.CANDIDATE.value
            highlight.error = None
            selected.append(highlight)
            continue
        highlight.status = HighlightStatus.REJECTED.value
        highlight.error = SELECTION_PREFIX + reason
    db.flush()
    if len(selected) < settings.highlights_per_stream_min and pool:
        log_activity(
            db,
            ActivityLevel.INFO,
            ActivityCategory.PIPELINE,
            f"Auswahl {group}: nur {len(selected)} Highlight(s) erreichen die Mindestbewertung "
            f"(Ziel {settings.highlights_per_stream_min}–{settings.highlights_per_stream_max})",
            streamer_id=pool[0].streamer_id,
        )
    return selected
