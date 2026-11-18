"""Chat-Aktivität als reine Zählwerte (DSGVO-sparsam).

Eingehende Chat-Nachrichten (EventSub channel.chat.message) werden sofort auf Zähler in Redis reduziert:
Anzahl, verschiedene Absender (HyperLogLog über gehashte IDs), Emotes, Hype-Schlüsselwörter je 10 s.
Texte, Namen und IDs werden nicht gespeichert. Ein Job überträgt fertige Zeitfenster in die Datenbank.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ChatActivity, Streamer, StreamSession

BUCKET_SECONDS = 10
PREFIX = "tcai:chat:"
KEY_TTL = 3600
HYPE = re.compile(
    r"\b(pog\w*|kekw|lul\w*|omegalul|w+|lets ?go+|gg+|clip+ ?it|wtf|omg|haha+|xd+"
    r"|krass|digga|alter|monka\w*)\b",
    re.IGNORECASE,
)


def record_chat_message(
    redis: Redis,
    broadcaster_id: str,
    chatter_id: str,
    text: str,
    emote_count: int,
    *,
    now: float | None = None,
) -> None:
    bucket = int((now or time.time()) // BUCKET_SECONDS * BUCKET_SECONDS)
    base = f"{PREFIX}{broadcaster_id}:{bucket}"
    anonymous = hashlib.sha256(f"{broadcaster_id}:{chatter_id}".encode()).hexdigest()[:16]
    pipe = redis.pipeline()
    pipe.hincrby(base, "n", 1)
    if emote_count:
        pipe.hincrby(base, "e", emote_count)
    if HYPE.search(text or ""):
        pipe.hincrby(base, "h", 1)
    pipe.pfadd(base + ":u", anonymous)
    pipe.expire(base, KEY_TTL)
    pipe.expire(base + ":u", KEY_TTL)
    pipe.execute()


def flush_chat_buckets(
    db: Session, redis: Redis, *, now: float | None = None, settle_seconds: int = 20
) -> int:
    """Abgeschlossene Zeitfenster aus Redis in chat_activity schreiben."""
    cutoff = (now or time.time()) - BUCKET_SECONDS - settle_seconds
    written = 0
    streamers: dict[str, Streamer | None] = {}
    for raw_key in redis.scan_iter(match=f"{PREFIX}*", count=500):
        key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
        if key.endswith(":u"):
            continue
        try:
            broadcaster_id, bucket_text = key[len(PREFIX) :].rsplit(":", 1)
            bucket = int(bucket_text)
        except ValueError:
            continue
        if bucket > cutoff:
            continue
        values = {(k.decode() if isinstance(k, bytes) else k): int(v) for k, v in redis.hgetall(key).items()}
        unique = int(redis.pfcount(key + ":u"))
        redis.delete(key, key + ":u")
        if broadcaster_id not in streamers:
            streamers[broadcaster_id] = db.scalar(
                select(Streamer).where(Streamer.twitch_user_id == broadcaster_id)
            )
        streamer = streamers[broadcaster_id]
        if streamer is None:
            continue
        start = datetime.fromtimestamp(bucket, tz=UTC)
        session = db.scalar(
            select(StreamSession)
            .where(StreamSession.streamer_id == streamer.id, StreamSession.started_at <= start)
            .order_by(StreamSession.started_at.desc())
            .limit(1)
        )
        row = db.scalar(
            select(ChatActivity).where(
                ChatActivity.streamer_id == streamer.id, ChatActivity.bucket_start == start
            )
        )
        if row is None:
            row = ChatActivity(
                streamer_id=streamer.id,
                bucket_start=start,
                bucket_seconds=BUCKET_SECONDS,
                message_count=0,
                unique_chatters=0,
                emote_count=0,
                hype_keyword_count=0,
            )
            db.add(row)
        row.stream_session_id = (
            session.id if session and (session.ended_at is None or session.ended_at >= start) else None
        )
        row.message_count += values.get("n", 0)
        row.unique_chatters = max(row.unique_chatters, unique)
        row.emote_count += values.get("e", 0)
        row.hype_keyword_count += values.get("h", 0)
        written += 1
    db.flush()
    return written


# --- Auswertung -------------------------------------------------------------
@dataclass
class ChatWindow:
    ratio: float  # Aktivität im Fenster relativ zum Median des Streams
    peak_per_minute: float
    hype: int


def _intensity(row: ChatActivity) -> float:
    return row.message_count + 2 * row.hype_keyword_count + 0.5 * row.emote_count


def session_buckets(db: Session, session: StreamSession) -> list[ChatActivity]:
    stmt = select(ChatActivity).where(
        ChatActivity.streamer_id == session.streamer_id, ChatActivity.bucket_start >= session.started_at
    )
    if session.ended_at:
        stmt = stmt.where(ChatActivity.bucket_start <= session.ended_at)
    return list(db.scalars(stmt.order_by(ChatActivity.bucket_start)).all())


def window_signal(
    buckets: list[ChatActivity], session: StreamSession, vod_start: float, vod_end: float
) -> ChatWindow | None:
    """Chat-Reaktion auf einen VOD-Ausschnitt. Die Reaktion folgt dem Geschehen leicht verzögert (+15 s)."""
    if len(buckets) < 6:
        return None
    values = sorted(_intensity(b) for b in buckets)
    median = max(1.0, values[len(values) // 2])
    start = session.started_at + timedelta(seconds=vod_start)
    end = session.started_at + timedelta(seconds=vod_end + 15)
    inside = [b for b in buckets if start <= b.bucket_start <= end]
    if not inside:
        return ChatWindow(ratio=0.0, peak_per_minute=0.0, hype=0)
    peak = max(_intensity(b) for b in inside)
    return ChatWindow(
        ratio=round(peak / median, 2),
        peak_per_minute=round(max(b.message_count for b in inside) * 60 / BUCKET_SECONDS, 1),
        hype=sum(b.hype_keyword_count for b in inside),
    )


def find_spikes(
    buckets: list[ChatActivity], session: StreamSession, factor: float, *, min_gap: int = 90, limit: int = 5
) -> list[float]:
    """VOD-Zeitpunkte (s) der stärksten Chat-Spitzen über factor × Median."""
    if len(buckets) < 12:
        return []
    values = sorted(_intensity(b) for b in buckets)
    median = max(1.0, values[len(values) // 2])
    peaks = sorted(
        ((b, _intensity(b)) for b in buckets if _intensity(b) >= factor * median), key=lambda x: -x[1]
    )
    chosen: list[float] = []
    for bucket, _value in peaks:
        offset = (bucket.bucket_start - session.started_at).total_seconds()
        if offset < 0 or any(abs(offset - c) < min_gap for c in chosen):
            continue
        chosen.append(offset)
        if len(chosen) >= limit:
            break
    return sorted(chosen)
