"""Untertitel im ASS-Format (libass) für 1080×1920 – groß, kontrastreich, Wort für Wort hervorgehoben.

- Kurze Zeilen (max. 3 Wörter / 22 Zeichen), damit sie auf dem Handy auch ohne Ton lesbar sind
- Das gerade gesprochene Wort wird gelb (ASS-Karaoke \\k), der Rest weiß mit dicker schwarzer Kontur
- Position oberhalb der TikTok-Bedienelemente bzw. an der Grenze Facecam/Gameplay
- Optionale Überschrift oben (dauerhaft oder nur in den ersten Sekunden)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.media.layout import OUT_H, OUT_W, Layout

FONT = "DejaVu Sans"
MAX_WORDS = 3
MAX_CHARS = 22
MAX_LINE_SECONDS = 1.8
PAUSE_SPLIT = 0.6
TITLE_SECONDS = 3.5


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    words: list[tuple[str, float, float]]  # (Text, Start, Ende)

    @property
    def text(self) -> str:
        return " ".join(w for w, _s, _e in self.words)


def _clean(text: str) -> str:
    return " ".join(text.replace("\n", " ").split())


def build_cues(words: list[dict], clip_duration: float) -> list[Cue]:
    """Gruppiert Wörter (relativ zum Ausschnitt) zu kurzen Untertitelzeilen."""
    items = [
        (_clean(w["text"]), max(0.0, float(w["start"])), min(clip_duration, float(w["end"])))
        for w in words
        if _clean(w.get("text", "")) and float(w["end"]) > 0 and float(w["start"]) < clip_duration
    ]
    cues: list[Cue] = []
    current: list[tuple[str, float, float]] = []
    for text, start, end in items:
        if current:
            joined = " ".join(t for t, _s, _e in current + [(text, start, end)])
            too_long = len(current) >= MAX_WORDS or len(joined) > MAX_CHARS
            too_slow = end - current[0][1] > MAX_LINE_SECONDS
            paused = start - current[-1][2] > PAUSE_SPLIT
            if too_long or too_slow or paused:
                cues.append(Cue(current[0][1], current[-1][2], current))
                current = []
        current.append((text, start, max(end, start + 0.12)))
    if current:
        cues.append(Cue(current[0][1], current[-1][2], current))
    # Lücken schließen: Zeile bleibt bis zur nächsten stehen (max. +0,4 s), nie überlappend
    fixed: list[Cue] = []
    for i, cue in enumerate(cues):
        next_start = cues[i + 1].start if i + 1 < len(cues) else clip_duration
        fixed.append(Cue(cue.start, min(next_start, cue.end + 0.4, clip_duration), cue.words))
    return fixed


def _ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def _karaoke(cue: Cue) -> str:
    parts = []
    cursor = cue.start
    for text, start, end in cue.words:
        gap = max(0.0, start - cursor)
        if gap > 0.01:
            parts.append(f"{{\\k{int(gap * 100)}}}")
        parts.append(f"{{\\k{max(1, int((end - start) * 100))}}}{_escape(text)}")
        cursor = end
    return " ".join(parts).replace("} {", "}{")


def render_ass(
    words: list[dict],
    clip_duration: float,
    layout: Layout,
    *,
    title: str | None = None,
    uppercase: bool = False,
) -> str:
    """Erzeugt den Inhalt einer .ass-Datei."""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{FONT},80,&H0000E5FF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,1,0,1,7,3,5,60,60,0,1
Style: Title,{FONT},64,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB4000000,-1,0,0,0,100,100,0,0,3,18,0,5,70,70,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""  # noqa: E501
    lines = [header.rstrip("\n")]
    if title:
        end = clip_duration if layout.title_persistent else min(TITLE_SECONDS, clip_duration)
        lines.append(
            f"Dialogue: 1,{_ts(0)},{_ts(end)},Title,,0,0,0,,"
            f"{{\\pos({OUT_W // 2},{layout.title_y})\\fad(200,300)}}"
            f"{_escape(_clean(title))}"
        )
    for cue in build_cues(words, clip_duration):
        cue_words = [(w.upper() if uppercase else w, s, e) for w, s, e in cue.words]
        text = _karaoke(Cue(cue.start, cue.end, cue_words))
        lines.append(
            f"Dialogue: 0,{_ts(cue.start)},{_ts(cue.end)},Caption,,0,0,0,,"
            f"{{\\pos({OUT_W // 2},{layout.subtitle_y})\\fad(60,60)}}{text}"
        )
    return "\n".join(lines) + "\n"


def to_srt(words: list[dict], clip_duration: float) -> str:
    """Zusätzlich als SRT (z. B. für manuellen Upload oder Barrierefreiheit)."""
    out = []
    for i, cue in enumerate(build_cues(words, clip_duration), start=1):
        out.append(f"{i}\n{_srt_ts(cue.start)} --> {_srt_ts(cue.end)}\n{cue.text}\n")
    return "\n".join(out)


def _srt_ts(seconds: float) -> str:
    millis = int(round(max(0.0, seconds) * 1000))
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_subtitles(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path
