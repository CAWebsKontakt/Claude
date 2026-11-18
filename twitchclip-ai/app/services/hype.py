"""Hype-Liste: Welche Kanäle wachsen gerade? (nur öffentliche Twitch-Daten, Modus A)

Ablauf je Lauf (Standard alle 15 min):
1. Get Streams: die meistgesehenen Live-Streams (Sprache/Kategorie konfigurierbar), gefiltert auf
   HYPE_MIN_VIEWERS–HYPE_MAX_VIEWERS.
2. Zuschauerzahl je Kanal als Stichprobe in Redis (6 h) → Wachstum gegenüber vor ~1 Stunde.
3. Für die vielversprechendsten Kanäle: Get Clips der letzten 24 h → Clip-Aktivität (Clips in den letzten
   3 Stunden, bester Clip).
4. Hype-Score 0–100 aus Wachstum (45 %), Clip-Aktivität (40 %) und passender Kanalgröße (15 %); fehlt die
   Zuschauer-Historie (erste Stunde nach dem Start), wird ihr Gewicht umverteilt. Nicht abgefragte Clips
   zählen als 0.

Es werden keine Inhalte heruntergeladen und niemand kontaktiert. Die Liste ist eine Entscheidungshilfe,
wen man höflich um Erlaubnis fragt (Dashboard → Hype-Liste → „Zustimmung anfragen“).
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.integrations.twitch.client import HelixClient, TwitchStream
from app.models import Streamer
from app.timeutil import utcnow

SAMPLE_PREFIX = "tcai:hype:viewers:"
LATEST_KEY = "tcai:hype:latest"
SAMPLE_RETENTION = timedelta(hours=6)
BASELINE_AGE = timedelta(minutes=60)
MIN_BASELINE_AGE = timedelta(minutes=20)
WEIGHTS = {"wachstum": 0.45, "clips": 0.40, "groesse": 0.15}


@dataclass
class HypeEntry:
    user_id: str
    login: str
    name: str
    game: str
    title: str
    viewers: int
    started_at: str | None
    viewers_before: int | None = None
    minutes_compared: int | None = None
    growth_pct: float | None = None
    clips_3h: int | None = None
    clips_24h: int | None = None
    top_clip_url: str | None = None
    top_clip_title: str | None = None
    top_clip_views: int | None = None
    score: float = 0.0
    components: dict = field(default_factory=dict)
    streamer_id: int | None = None
    streamer_mode: str | None = None

    @property
    def uptime_minutes(self) -> int | None:
        if not self.started_at:
            return None
        return max(0, int((utcnow() - datetime.fromisoformat(self.started_at)).total_seconds() // 60))


@dataclass
class HypeResult:
    at: str
    scanned: int
    in_range: int
    entries: list[HypeEntry]
    settings: dict

    def to_json(self) -> str:
        return json.dumps({**asdict(self), "entries": [asdict(e) for e in self.entries]})

    @classmethod
    def from_json(cls, raw: str | bytes) -> HypeResult:
        data = json.loads(raw)
        return cls(
            at=data["at"],
            scanned=data["scanned"],
            in_range=data["in_range"],
            entries=[HypeEntry(**e) for e in data["entries"]],
            settings=data.get("settings", {}),
        )

    @property
    def at_datetime(self) -> datetime:
        return datetime.fromisoformat(self.at)


# ---------------------------------------------------------------------------
# Zuschauer-Stichproben
# ---------------------------------------------------------------------------
def record_sample(redis: Redis, user_id: str, viewers: int, now: datetime) -> None:
    key = SAMPLE_PREFIX + user_id
    ts = now.timestamp()
    pipe = redis.pipeline()
    pipe.zadd(key, {f"{int(ts)}:{viewers}": ts})
    pipe.zremrangebyscore(key, 0, ts - SAMPLE_RETENTION.total_seconds())
    pipe.expire(key, int(SAMPLE_RETENTION.total_seconds()) + 3600)
    pipe.execute()


def baseline(redis: Redis, user_id: str, now: datetime) -> tuple[int, int] | None:
    """(Zuschauer, Minuten zurück) der Stichprobe am nächsten an „vor 1 Stunde“ (mind. 20 min alt)."""
    newest_allowed = (now - MIN_BASELINE_AGE).timestamp()
    rows = redis.zrangebyscore(SAMPLE_PREFIX + user_id, 0, newest_allowed, withscores=True)
    if not rows:
        return None
    target = (now - BASELINE_AGE).timestamp()
    member, ts = min(rows, key=lambda row: abs(row[1] - target))
    member = member.decode() if isinstance(member, bytes) else member
    viewers = int(member.split(":", 1)[1])
    return viewers, int((now.timestamp() - ts) // 60)


# ---------------------------------------------------------------------------
# Bewertung
# ---------------------------------------------------------------------------
def growth_component(growth_pct: float | None) -> float | None:
    """+100 % in einer Stunde = 1.0; Rückgang = 0."""
    if growth_pct is None:
        return None
    return max(0.0, min(1.0, growth_pct / 100))


def clip_component(clips_3h: int | None) -> float | None:
    """Logarithmisch: 1 Clip ≈ 0.3, 5 ≈ 0.75, ≥ 10 = 1.0 (Zuschauer clippen, wenn etwas passiert)."""
    if clips_3h is None:
        return None
    return min(1.0, math.log1p(clips_3h) / math.log1p(10))


def size_component(viewers: int) -> float:
    """Mittelgroße Kanäle antworten eher und profitieren am meisten – 200–5000 Zuschauer = 1.0."""
    if viewers < 200:
        return max(0.2, viewers / 200)
    if viewers <= 5000:
        return 1.0
    return max(0.2, 1 - (viewers - 5000) / 20000)


def score(entry: HypeEntry) -> float:
    """Fehlt der Zuschauer-Verlauf (erste Stunde nach dem Start), wird sein Gewicht umverteilt.
    Nicht abgefragte Clips zählen als 0 – sonst stünde ein Kanal ohne jedes Signal allein wegen seiner
    Größe ganz oben."""
    parts = {
        "wachstum": growth_component(entry.growth_pct),
        "clips": clip_component(entry.clips_3h) or 0.0,
        "groesse": size_component(entry.viewers),
    }
    available = {k: v for k, v in parts.items() if v is not None}
    total_weight = sum(WEIGHTS[k] for k in available)
    entry.components = {k: round(v, 2) for k, v in available.items()}
    entry.score = round(100 * sum(WEIGHTS[k] * v for k, v in available.items()) / total_weight, 1)
    return entry.score


# ---------------------------------------------------------------------------
# Lauf
# ---------------------------------------------------------------------------
def _game_ids(settings: Settings) -> list[str]:
    return [g.strip() for g in settings.hype_game_ids.split(",") if g.strip()]


def _entry(stream: TwitchStream) -> HypeEntry:
    return HypeEntry(
        user_id=stream.user_id,
        login=stream.user_login,
        name=stream.user_name or stream.user_login,
        game=stream.game_name,
        title=stream.title[:140],
        viewers=stream.viewer_count,
        started_at=stream.started_at.isoformat() if stream.started_at else None,
    )


def scan(
    db: Session, settings: Settings, redis: Redis, client: HelixClient, now: datetime | None = None
) -> HypeResult:
    now = now or utcnow()
    streams = client.get_top_streams(
        language=settings.hype_language or None, game_ids=_game_ids(settings), max_pages=settings.hype_pages
    )
    seen: set[str] = set()
    entries: list[HypeEntry] = []
    for stream in streams:
        if stream.user_id in seen:  # Paginierung kann bei wechselnden Zahlen doppeln
            continue
        seen.add(stream.user_id)
        record_sample(redis, stream.user_id, stream.viewer_count, now)
        if not settings.hype_min_viewers <= stream.viewer_count <= settings.hype_max_viewers:
            continue
        entry = _entry(stream)
        base = baseline(redis, stream.user_id, now)
        if base is not None:
            entry.viewers_before, entry.minutes_compared = base
            entry.growth_pct = round(100 * (entry.viewers - base[0]) / max(base[0], 50), 1)
        entries.append(entry)

    # Clip-Aktivität nur für die vielversprechendsten (spart API-Aufrufe)
    def prelim(e: HypeEntry) -> tuple:
        return (e.growth_pct is not None, e.growth_pct or 0.0, size_component(e.viewers))

    for entry in sorted(entries, key=prelim, reverse=True)[: settings.hype_clip_checks]:
        clips = client.get_clips(entry.user_id, now - timedelta(hours=24), now, max_pages=1)
        recent = [c for c in clips if c.created_at >= now - timedelta(hours=3)]
        entry.clips_24h = len(clips)
        entry.clips_3h = len(recent)
        best = max(recent or clips, key=lambda c: c.view_count, default=None)
        if best is not None:
            entry.top_clip_url, entry.top_clip_title, entry.top_clip_views = (
                best.url,
                best.title[:120],
                best.view_count,
            )

    in_range = len(entries)
    for entry in entries:
        score(entry)
    entries.sort(key=lambda e: e.score, reverse=True)
    entries = entries[: settings.hype_list_size]

    known = {
        s.twitch_user_id: s
        for s in db.scalars(select(Streamer).where(Streamer.twitch_user_id.in_([e.user_id for e in entries])))
    }
    for entry in entries:
        if entry.user_id in known:
            entry.streamer_id = known[entry.user_id].id
            entry.streamer_mode = known[entry.user_id].mode

    result = HypeResult(
        at=now.isoformat(),
        scanned=len(seen),
        in_range=in_range,
        entries=entries,
        settings={
            "language": settings.hype_language or "alle",
            "min": settings.hype_min_viewers,
            "max": settings.hype_max_viewers,
        },
    )
    redis.set(LATEST_KEY, result.to_json(), ex=3 * 3600)
    return result


def latest(redis: Redis) -> HypeResult | None:
    try:
        raw = redis.get(LATEST_KEY)
    except RedisError:
        return None
    return HypeResult.from_json(raw) if raw else None


def find_entry(redis: Redis, user_id: str) -> HypeEntry | None:
    result = latest(redis)
    if result is None:
        return None
    return next((e for e in result.entries if e.user_id == user_id), None)
