"""Redis-Verbindung (für RQ ohne decode_responses – RQ erwartet Bytes)."""

from __future__ import annotations

from redis import Redis

from app.config import get_settings

_redis: Redis | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(
            get_settings().redis_url,
            socket_connect_timeout=5,
            socket_timeout=10,
            health_check_interval=30,
        )
    return _redis


def set_redis(client: Redis | None) -> None:
    """Für Tests: z. B. fakeredis einsetzen."""
    global _redis
    _redis = client
