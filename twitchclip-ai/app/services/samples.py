"""Erzeugt synthetische Beispielvideos für den Testmodus – ohne jegliche Fremdinhalte.

Aufbau jedes Videos (1920×1080, 30 fps, H.264/AAC):
- „Gameplay“: FFmpeg-Testbild (testsrc2, bewegte Farbflächen + Zähler)
- „Facecam“: animiertes Fraktal unten rechts (simuliert eine Webcam-Einblendung)
- Ton: leiser Grundton mit mehreren lauten Abschnitten („Reaktionen“) – damit lässt sich die
  Lautheitsanalyse ab Phase 4 überprüfen.
Alle Inhalte werden mathematisch erzeugt; es bestehen keine Rechte Dritter.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.config import Settings


class SampleGenerationError(RuntimeError):
    pass


def _audio_expression(duration: int, variant: int) -> str:
    # Laute Abschnitte („Reaktionen“) an variantenabhängigen Stellen, je 3 s lang
    peaks = [round(duration * f) for f in ((0.25, 0.7), (0.4, 0.85), (0.15, 0.55))[variant % 3]]
    loud = "+".join(f"between(t,{p},{p + 3})" for p in peaks)
    return f"(0.04+0.55*({loud}))*sin(2*PI*(220+80*sin(2*PI*0.7*t))*t)+0.02*sin(2*PI*{330 + 40 * variant}*t)"


def generate_samples(
    settings: Settings, *, count: int = 3, duration: int = 60, overwrite: bool = False, tag: str | None = None
) -> list[Path]:
    """tag: eindeutiger Namensbestandteil (Selbsttest) – Kennung landet auch in den Metadaten,
    damit sich die Dateien per SHA-256 unterscheiden."""
    if shutil.which("ffmpeg") is None:
        raise SampleGenerationError("ffmpeg ist nicht installiert")
    if not 10 <= duration <= 600:
        raise SampleGenerationError("Dauer muss zwischen 10 und 600 Sekunden liegen")
    settings.samples_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for index in range(count):
        target = settings.samples_dir / f"{tag or 'beispiel'}-{index + 1:02d}-{duration}s.mp4"
        if target.exists() and not overwrite:
            continue
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30:duration={duration}",
            "-f", "lavfi", "-i", f"mandelbrot=size=480x270:rate=30:start_scale={2 + index}",
            "-f", "lavfi", "-i", f"aevalsrc='{_audio_expression(duration, index)}':s=48000:d={duration}",
            "-filter_complex", "[1:v]format=yuv420p[cam];[0:v][cam]overlay=W-w-48:H-h-48:shortest=1[v]",
            "-map", "[v]", "-map", "2:a",
            "-t", str(duration),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-metadata", f"comment=TwitchClip AI {tag or 'beispiel'}",
            "-movflags", "+faststart",
            str(target),
        ]  # fmt: skip
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=duration * 10 + 60)
        except subprocess.CalledProcessError as exc:
            target.unlink(missing_ok=True)
            raise SampleGenerationError(f"ffmpeg-Fehler: {exc.stderr[-500:]}") from exc
        except subprocess.TimeoutExpired as exc:
            target.unlink(missing_ok=True)
            raise SampleGenerationError("ffmpeg hat zu lange gebraucht") from exc
        created.append(target)
    return created
