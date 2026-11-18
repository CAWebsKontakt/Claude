"""Rendering mit FFmpeg: 1080×1920, H.264 (High, yuv420p, 30 fps), AAC 48 kHz, Lautheit normalisiert.

Es wird keine Musik hinzugefügt – nur der Original-Ton des (berechtigten) Clips, auf −14 LUFS normalisiert
(übliche Zielhöhe für Kurzvideo-Plattformen, True Peak −1,5 dBTP).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.media.layout import OUT_H, OUT_W, Layout
from app.services.media_probe import probe_media, sha256_file

logger = logging.getLogger(__name__)

TARGET_LUFS = -14
TRUE_PEAK = -1.5
FPS = 30


class RenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class RenderResult:
    path: Path
    thumbnail: Path
    duration: float
    size_bytes: int
    sha256: str
    width: int
    height: int


def _filter_path(path: Path) -> str:
    """Pfad für FFmpeg-Filterargumente escapen (Doppelpunkt, Backslash, Apostroph)."""
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_command(
    source: Path,
    target: Path,
    *,
    start: float,
    end: float,
    layout: Layout,
    subtitles: Path | None,
    has_audio: bool,
    preset: str = "medium",
    crf: int = 20,
) -> list[str]:
    duration = max(0.5, end - start)
    video_graph = layout.filter_graph
    if subtitles is not None:
        video_graph = video_graph.replace(
            "[v0]", "[vlay];[vlay]ass=filename='" + _filter_path(subtitles) + "'[v0]", 1
        )
    video_graph += f";[v0]fps={FPS},format=yuv420p[vout]"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source)]  # fmt: skip
    if has_audio:
        audio_graph = (
            f"[0:a]aresample=48000,loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK}:LRA=11,"
            "aformat=sample_rates=48000:channel_layouts=stereo[aout]"
        )
    else:
        # Stille Tonspur, damit die Datei immer H.264 + AAC enthält
        command += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        audio_graph = "[1:a]anull[aout]"
    command += [
        "-filter_complex", f"{video_graph};{audio_graph}",
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-profile:v", "high", "-level", "4.1",
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", "-t", f"{duration:.3f}",
        str(target),
    ]  # fmt: skip
    return command


def render_video(
    source: Path,
    target: Path,
    *,
    start: float,
    end: float,
    layout: Layout,
    subtitles: Path | None,
    has_audio: bool,
    thumbnail_at: float | None = None,
    timeout: int = 1800,
    preset: str = "medium",
    crf: int = 20,
) -> RenderResult:
    if shutil.which("ffmpeg") is None:
        raise RenderError("ffmpeg ist nicht installiert")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_target = target.with_suffix(".part.mp4")
    command = build_command(source, tmp_target, start=start, end=end, layout=layout, subtitles=subtitles,
                            has_audio=has_audio, preset=preset, crf=crf)  # fmt: skip
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        tmp_target.unlink(missing_ok=True)
        raise RenderError(f"FFmpeg-Fehler: {exc.stderr[-600:]}") from exc
    except subprocess.TimeoutExpired as exc:
        tmp_target.unlink(missing_ok=True)
        raise RenderError("Rendering dauerte zu lange") from exc

    info = probe_media(tmp_target)
    if (
        not info
        or info["width"] != OUT_W
        or info["height"] != OUT_H
        or info["video_codec"] != "h264"
        or info["audio_codec"] != "aac"
    ):
        tmp_target.unlink(missing_ok=True)
        raise RenderError(f"Ausgabe entspricht nicht 1080×1920 H.264/AAC: {info}")
    tmp_target.replace(target)

    thumbnail = target.with_suffix(".jpg")
    at = min(
        max(0.0, thumbnail_at if thumbnail_at is not None else info["duration"] / 2),
        max(0.0, info["duration"] - 0.1),
    )
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{at:.2f}",
                "-i",
                str(target),
                "-frames:v",
                "1",
                "-vf",
                "scale=540:-2",
                "-q:v",
                "3",
                str(thumbnail),
            ],  # fmt: skip
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RenderError(f"Vorschaubild fehlgeschlagen: {exc}") from exc
    return RenderResult(
        path=target,
        thumbnail=thumbnail,
        duration=float(info["duration"]),
        size_bytes=target.stat().st_size,
        sha256=sha256_file(target),
        width=info["width"],
        height=info["height"],
    )
