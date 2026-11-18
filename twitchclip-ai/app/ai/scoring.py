"""Highlight-Bewertung 0–100 und Wahl des besten Ausschnitts (20–45 s)."""

from __future__ import annotations

import math
from dataclasses import dataclass

WEIGHTS = {
    "ton": 0.25,
    "sprache": 0.20,
    "chat": 0.20,
    "bild": 0.15,
    "zuschauer": 0.10,
    "laenge": 0.10,
}
LABELS = {
    "ton": "Ton (Lautstärke-Ausbrüche)",
    "sprache": "Sprache (Ausrufe, Schlüsselwörter, Lachen)",
    "chat": "Chat-Reaktion",
    "bild": "Bild (Bewegung, Schnitte)",
    "zuschauer": "Zuschauer-Interesse (Clip-Bewertung)",
    "laenge": "Passende Länge",
}


@dataclass
class Window:
    start: float
    end: float
    strength: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def combine_series(*series: tuple[list[float], float]) -> list[float]:
    length = max((len(s) for s, _w in series), default=0)
    total = [0.0] * length
    for values, weight in series:
        for i, v in enumerate(values):
            total[i] += weight * v
    return total


def best_window(signal: list[float], duration: float, min_len: int, max_len: int) -> Window:
    """Fenster mit der höchsten (spitzengewichteten) Aktivität; ganze Datei, wenn sie kurz genug ist."""
    if duration <= max_len or not signal:
        strength = _strength(signal)
        return Window(0.0, round(duration, 2), strength)
    best = Window(0.0, float(max_len), -1.0)
    length = max_len
    for start in range(0, max(1, int(duration) - length + 1)):
        part = signal[start : start + length]
        strength = _strength(part)
        if strength > best.strength:
            best = Window(float(start), float(start + length), strength)
    # Ruhige Anfangssekunden abschneiden, solange die Mindestlänge bleibt (Clips sollen schnell starten)
    start, end = int(best.start), int(best.end)
    while end - start > min_len and signal[start] < 0.15 * max(signal[start:end] or [1]):
        start += 1
    return Window(float(start), float(end), best.strength)


def _strength(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values, reverse=True)
    top = ordered[: max(1, len(ordered) // 5)]
    return 0.6 * (sum(top) / len(top)) + 0.4 * (sum(values) / len(values))


def window_component(values: list[float], start: float, end: float) -> float:
    part = values[int(start) : max(int(start) + 1, int(math.ceil(end)))]
    return round(min(1.0, _strength(part)), 3)


def duration_fit(length: float, minimum: int, maximum: int) -> float:
    if minimum <= length <= maximum:
        return 1.0
    distance = minimum - length if length < minimum else length - maximum
    return round(max(0.0, 1.0 - distance / 15.0), 3)


def chat_component(ratio: float | None) -> float | None:
    """Verhältnis Fenster/Median → 0..1 (1× = 0, 6× und mehr = 1)."""
    if ratio is None:
        return None
    if ratio <= 1:
        return 0.0
    return round(min(1.0, math.log(ratio) / math.log(6)), 3)


def total_score(components: dict[str, float | None]) -> tuple[float, dict[str, float]]:
    """Gewichtete Summe; fehlende Signale (None) werden auf die vorhandenen umverteilt."""
    available = {k: v for k, v in components.items() if v is not None and k in WEIGHTS}
    weight_sum = sum(WEIGHTS[k] for k in available) or 1.0
    score = 100 * sum(WEIGHTS[k] * v for k, v in available.items()) / weight_sum
    breakdown = {LABELS[k]: round(v, 2) for k, v in available.items()}
    missing = [LABELS[k] for k, v in components.items() if v is None and k in WEIGHTS]
    if missing:
        breakdown["nicht verfügbar"] = ", ".join(missing)
    return round(max(0.0, min(100.0, score)), 1), breakdown
