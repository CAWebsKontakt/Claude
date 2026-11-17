"""Login-Schutz gegen Brute-Force: zählt Fehlversuche pro IP und pro Benutzername in Redis."""

from __future__ import annotations

import hashlib
import logging
from contextlib import suppress

from redis import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


def _key(kind: str, value: str) -> str:
    digest = hashlib.sha256(value.lower().encode("utf-8")).hexdigest()[:32]
    return f"tcai:login-fail:{kind}:{digest}"


class LoginRateLimiter:
    def __init__(self, redis: Redis, max_attempts: int, window_seconds: int) -> None:
        self.redis = redis
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds

    def _keys(self, ip: str, username: str) -> list[str]:
        return [_key("ip", ip), _key("user", username or "-")]

    def is_blocked(self, ip: str, username: str) -> bool:
        try:
            values = self.redis.mget(self._keys(ip, username))
        except RedisError:
            # Redis nicht erreichbar: lieber blockieren als Brute-Force zulassen.
            logger.error("Redis nicht erreichbar – Login vorübergehend gesperrt")
            return True
        # Pro IP etwas großzügiger (mehrere Nutzer hinter einem NAT).
        ip_count = int(values[0] or 0)
        user_count = int(values[1] or 0)
        return ip_count >= self.max_attempts * 3 or user_count >= self.max_attempts

    def register_failure(self, ip: str, username: str) -> None:
        try:
            pipe = self.redis.pipeline()
            for key in self._keys(ip, username):
                pipe.incr(key)
                pipe.expire(key, self.window_seconds)
            pipe.execute()
        except RedisError:
            logger.error("Fehlversuch konnte nicht gezählt werden (Redis)")

    def reset(self, ip: str, username: str) -> None:
        with suppress(RedisError):
            self.redis.delete(_key("user", username or "-"))
