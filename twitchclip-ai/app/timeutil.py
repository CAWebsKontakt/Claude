"""Zeit-Hilfsfunktionen. Intern wird ausschließlich UTC gespeichert, angezeigt wird in TIMEZONE."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_local(value: datetime | None, tz: ZoneInfo) -> datetime | None:
    if value is None:
        return None
    return ensure_utc(value).astimezone(tz)


def local_day_bounds(tz: ZoneInfo, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Beginn und Ende des aktuellen Kalendertags in der lokalen Zeitzone, als UTC-Zeitpunkte."""
    now_local = ensure_utc(now or utcnow()).astimezone(tz)
    start_local = datetime.combine(now_local.date(), time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def humanize_delta(value: datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return "–"
    seconds = int((ensure_utc(now or utcnow()) - ensure_utc(value)).total_seconds())
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        text = "wenigen Sekunden"
    elif seconds < 3600:
        minutes = seconds // 60
        text = f"{minutes} Minute" + ("n" if minutes != 1 else "")
    elif seconds < 86400:
        hours = seconds // 3600
        text = f"{hours} Stunde" + ("n" if hours != 1 else "")
    else:
        days = seconds // 86400
        text = f"{days} Tag" + ("en" if days != 1 else "")
    return f"in {text}" if future else f"vor {text}"
