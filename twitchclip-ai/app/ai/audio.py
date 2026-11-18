"""Tonanalyse: Lautheitsverlauf, plötzliche Ausbrüche (Schreie, Jubel, Lachen) über FFmpeg + numpy."""

from __future__ import annotations

import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16_000
WINDOW_SECONDS = 0.5
SILENCE_DB = -70.0


class AudioError(RuntimeError):
    pass


def extract_audio(video: Path, target: Path, *, timeout: int = 300) -> Path:
    """Mono-WAV mit 16 kHz – Format für Lautheitsanalyse und Whisper."""
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "wav", str(target),
    ]  # fmt: skip
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        raise AudioError(f"Audio konnte nicht extrahiert werden: {exc.stderr[-300:]}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError(f"FFmpeg nicht verfügbar oder zu langsam: {exc}") from exc
    return target


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
        width = handle.getsampwidth()
    if width != 2:
        raise AudioError("Unerwartetes WAV-Format")
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, rate


def loudness_db(samples: np.ndarray, rate: int, window: float = WINDOW_SECONDS) -> np.ndarray:
    """RMS-Pegel in dBFS je Fenster."""
    size = max(1, int(rate * window))
    count = len(samples) // size
    if count == 0:
        return np.array([SILENCE_DB], dtype=np.float32)
    frames = samples[: count * size].reshape(count, size)
    rms = np.sqrt(np.mean(frames**2, axis=1))
    return np.maximum(20 * np.log10(np.maximum(rms, 1e-7)), SILENCE_DB).astype(np.float32)


@dataclass
class AudioProfile:
    window_seconds: float
    db: list[float]
    median_db: float
    peak_db: float
    # Aufregung je Sekunde 0..1 (Pegel über Median + plötzlicher Anstieg)
    excitement: list[float] = field(default_factory=list)
    spikes: list[float] = field(default_factory=list)  # Zeitpunkte (s) deutlicher Ausbrüche

    @property
    def has_audio(self) -> bool:
        return self.peak_db > -60


def analyze_samples(samples: np.ndarray, rate: int) -> AudioProfile:
    db = loudness_db(samples, rate)
    voiced = db[db > -60]
    median = float(np.median(voiced)) if voiced.size else SILENCE_DB
    peak = float(db.max()) if db.size else SILENCE_DB
    above = np.clip((db - median) / 18.0, 0.0, 1.0)  # 18 dB über Median = Maximum
    onset = np.clip(np.diff(db, prepend=db[:1]) / 12.0, 0.0, 1.0)  # plötzlicher Anstieg
    combined = np.clip(0.75 * above + 0.25 * onset, 0.0, 1.0)
    per_second = _resample_to_seconds(combined, WINDOW_SECONDS)
    spikes = [
        round(i * WINDOW_SECONDS, 1)
        for i in range(1, len(db))
        if db[i] > median + 10 and db[i - 1] <= median + 10
    ]
    return AudioProfile(
        window_seconds=WINDOW_SECONDS,
        db=[round(float(v), 1) for v in db],
        median_db=round(median, 1),
        peak_db=round(peak, 1),
        excitement=[round(float(v), 3) for v in per_second],
        spikes=spikes,
    )


def analyze_audio_file(wav: Path) -> AudioProfile:
    samples, rate = load_wav(wav)
    return analyze_samples(samples, rate)


def _resample_to_seconds(values: np.ndarray, window: float) -> np.ndarray:
    per = max(1, round(1 / window))
    seconds = int(np.ceil(len(values) / per))
    padded = np.zeros(seconds * per, dtype=np.float32)
    padded[: len(values)] = values
    return padded.reshape(seconds, per).max(axis=1)
