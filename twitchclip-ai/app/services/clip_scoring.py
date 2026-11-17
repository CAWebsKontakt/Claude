"""Bewertung öffentlicher Twitch-Clips (Modus A) auf einer Skala von 0 bis 100.

Grundlage sind ausschließlich Metadaten der Helix-API. Die Bewertung ist relativ zum jeweiligen Kanal
(ein Clip mit 300 Aufrufen ist bei einem kleinen Kanal ein Highlight, bei einem großen nicht).

Komponenten (Gewicht):
- Tempo (35 %): Aufrufe pro Stunde seit Erstellung, logarithmisch, relativ zum 90.-Perzentil des Kanals
- Beliebtheit (20 %): absolute Aufrufe, logarithmisch, relativ zum 90.-Perzentil des Kanals
- Aktualität (15 %): Halbwertszeit 24 Stunden
- Häufung (15 %): weitere Clips desselben VOD-Moments (±60 s) – mehrere Zuschauer haben ihn geclippt
- Länge (10 %): passt in CLIP_DURATION_MIN…CLIP_DURATION_MAX
- Featured (5 %): vom Kanal hervorgehoben
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

WEIGHTS = {
    "tempo": 0.35,
    "beliebtheit": 0.20,
    "aktualitaet": 0.15,
    "haeufung": 0.15,
    "laenge": 0.10,
    "featured": 0.05,
}
RECENCY_HALF_LIFE_HOURS = 24.0
CLUSTER_WINDOW_SECONDS = 60
CLUSTER_SATURATION = 3


@dataclass(frozen=True)
class ClipFeatures:
    key: str
    view_count: int
    created_at: datetime
    duration: float
    is_featured: bool
    video_id: str | None
    vod_offset: int | None


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def _log_ratio(value: float, reference: float) -> float:
    return min(1.0, math.log1p(max(0.0, value)) / math.log1p(max(reference, 1.0)))


def duration_fit(duration: float, minimum: int, maximum: int) -> float:
    if minimum <= duration <= maximum:
        return 1.0
    distance = minimum - duration if duration < minimum else duration - maximum
    return max(0.0, 1.0 - distance / 15.0)


def cluster_counts(clips: list[ClipFeatures]) -> dict[str, int]:
    """Anzahl anderer Clips desselben VODs, deren vod_offset höchstens 60 s entfernt liegt."""
    by_video: dict[str, list[ClipFeatures]] = defaultdict(list)
    for clip in clips:
        if clip.video_id and clip.vod_offset is not None:
            by_video[clip.video_id].append(clip)
    counts = {clip.key: 0 for clip in clips}
    for group in by_video.values():
        for clip in group:
            counts[clip.key] = sum(
                1
                for other in group
                if other.key != clip.key and abs(other.vod_offset - clip.vod_offset) <= CLUSTER_WINDOW_SECONDS
            )
    return counts


def score_clips(
    clips: list[ClipFeatures], *, now: datetime, duration_min: int, duration_max: int
) -> dict[str, tuple[float, dict[str, float]]]:
    """Bewertet alle Clips eines Kanals gemeinsam. Rückgabe: key -> (Score 0–100, Aufschlüsselung)."""
    if not clips:
        return {}
    ages = {c.key: max(0.25, (now - c.created_at).total_seconds() / 3600) for c in clips}
    velocities = {c.key: c.view_count / ages[c.key] for c in clips}
    ref_views = max(10.0, _percentile([float(c.view_count) for c in clips], 0.9))
    ref_velocity = max(5.0, _percentile(list(velocities.values()), 0.9))
    clusters = cluster_counts(clips)

    result: dict[str, tuple[float, dict[str, float]]] = {}
    for clip in clips:
        parts = {
            "tempo": _log_ratio(velocities[clip.key], ref_velocity),
            "beliebtheit": _log_ratio(clip.view_count, ref_views),
            "aktualitaet": 0.5 ** (ages[clip.key] / RECENCY_HALF_LIFE_HOURS),
            "haeufung": min(1.0, clusters[clip.key] / CLUSTER_SATURATION),
            "laenge": duration_fit(clip.duration, duration_min, duration_max),
            "featured": 1.0 if clip.is_featured else 0.0,
        }
        score = round(100 * sum(WEIGHTS[name] * value for name, value in parts.items()), 1)
        breakdown = {name: round(value, 2) for name, value in parts.items()}
        breakdown["aufrufe_pro_stunde"] = round(velocities[clip.key], 1)
        breakdown["aehnliche_clips"] = clusters[clip.key]
        result[clip.key] = (score, breakdown)
    return result
