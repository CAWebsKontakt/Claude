"""Spracherkennung mit faster-whisper (lokal auf dem VPS, keine Cloud).

Das Modell wird beim ersten Einsatz einmalig nach DATA_DIR/models geladen (z. B. "small" ≈ 480 MB).
Fehlt faster-whisper oder das Modell, läuft die Analyse ohne Transkript weiter.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    language: str | None
    words: list[Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text.strip() for w in self.words).strip()

    def between(self, start: float, end: float) -> list[Word]:
        return [w for w in self.words if w.end > start and w.start < end]


class Transcriber(Protocol):
    def transcribe(self, wav: Path, language: str | None) -> Transcript: ...


class WhisperTranscriber:
    def __init__(self, settings: Settings) -> None:
        from faster_whisper import WhisperModel  # schwerer Import erst bei Bedarf

        settings.models_dir.mkdir(parents=True, exist_ok=True)
        device = settings.whisper_device
        self.model = WhisperModel(
            settings.whisper_model,
            device=device,
            compute_type=settings.whisper_compute_type,
            download_root=str(settings.models_dir),
        )

    def transcribe(self, wav: Path, language: str | None) -> Transcript:
        segments, info = self.model.transcribe(
            str(wav), language=language, word_timestamps=True, vad_filter=True, beam_size=1
        )
        words: list[Word] = []
        for segment in segments:
            for word in segment.words or []:
                words.append(Word(start=float(word.start), end=float(word.end), text=word.word))
        return Transcript(language=info.language, words=words)


_override: Transcriber | None = None


def set_transcriber(transcriber: Transcriber | None) -> None:
    """Für Tests: festen Transkribierer einsetzen."""
    global _override
    _override = transcriber


def get_transcriber(settings: Settings) -> Transcriber | None:
    if _override is not None:
        return _override
    if not settings.analysis_transcription:
        return None
    try:
        return WhisperTranscriber(settings)
    except Exception as exc:  # noqa: BLE001 – Analyse soll ohne Transkript weiterlaufen
        logger.warning("Spracherkennung nicht verfügbar", extra={"error": str(exc)[:300]})
        return None


# --- Sprachsignale -----------------------------------------------------------
EXCITEMENT_WORDS = {
    "de": [
        "krass",
        "alter",
        "digga",
        "nein",
        "was",
        "wie",
        "geil",
        "boah",
        "hammer",
        "unglaublich",
        "wahnsinn",
        "oh mein gott",
        "junge",
        "los",
        "ja",
        "auf keinen fall",
        "heilig",
        "brutal",
        "lol",
        "haha",
    ],
    "en": [
        "omg",
        "oh my god",
        "no way",
        "what",
        "wtf",
        "lets go",
        "let's go",
        "insane",
        "crazy",
        "holy",
        "yes",
        "no",
        "dude",
        "bro",
        "clutch",
        "lol",
        "haha",
        "wow",
    ],
}
_LAUGH = re.compile(r"\b(ha){2,}\b|\b(he){2,}\b|lachen|laughs?", re.IGNORECASE)


def speech_excitement(transcript: Transcript | None, duration: float) -> list[float]:
    """Aufregung der Sprache je Sekunde (0..1): Schlüsselwörter, Ausrufe, Lachen, Sprechtempo."""
    seconds = max(1, int(duration + 0.999))
    values = [0.0] * seconds
    if transcript is None or not transcript.words:
        return values
    language = (transcript.language or "de")[:2]
    keywords = EXCITEMENT_WORDS.get(language, []) + EXCITEMENT_WORDS["en"]
    for word in transcript.words:
        token = word.text.strip().lower()
        i = min(seconds - 1, max(0, int(word.start)))
        score = 0.08  # Sprechen an sich
        clean = re.sub(r"[^\wäöüß' ]", "", token)
        if clean in keywords:
            score += 0.35
        if "!" in token or "?!" in token:
            score += 0.25
        if _LAUGH.search(token):
            score += 0.4
        if token.isupper() and len(token) > 2:
            score += 0.2
        values[i] = min(1.0, values[i] + score)
    # Mehrwort-Ausdrücke ("oh mein gott", "let's go")
    text = " ".join(w.text.strip().lower() for w in transcript.words)
    for phrase in (k for k in keywords if " " in k):
        for match in re.finditer(re.escape(phrase), text):
            position = len(text[: match.start()].split())
            if position < len(transcript.words):
                i = min(seconds - 1, int(transcript.words[position].start))
                values[i] = min(1.0, values[i] + 0.35)
    return [round(v, 3) for v in values]
