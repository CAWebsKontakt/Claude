"""Gemeinsames Rate-Limit-Gedächtnis für alle Prozesse (Redis).

Twitch meldet in jeder Antwort das verbleibende Kontingent (`Ratelimit-Remaining`) und den Zeitpunkt
des Zurücksetzens (`Ratelimit-Reset`, Unix-Zeit). Diese Werte werden je Token-Bucket in Redis
abgelegt. Vor jeder Anfrage wird geprüft, ob noch ein Sicherheitspuffer frei ist; andernfalls wird
kurz gewartet oder – bei längerer Wartezeit – ein TwitchRateLimitedError ausgelöst, damit RQ den Job
später wiederholt, statt einen Worker zu blockieren.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping

from redis import Redis
from redis.exceptions import RedisError

from app.integrations.twitch.errors import TwitchRateLimitedError

logger = logging.getLogger(__name__)

KEY_PREFIX = "tcai:twitch:ratelimit:"
# So viele Punkte bleiben für andere Prozesse/Webhook-bedingte Anfragen frei.
RESERVE_POINTS = 10
# Bis zu dieser Wartezeit wird im Prozess gewartet, darüber an RQ zurückgegeben.
MAX_INLINE_WAIT_SECONDS = 15.0


class RateLimiter:
    def __init__(
        self,
        redis: Redis,
        bucket: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.redis = redis
        self.key = KEY_PREFIX + bucket
        self.sleep = sleep
        self.clock = clock

    def state(self) -> dict[str, float] | None:
        try:
            raw = self.redis.hgetall(self.key)
        except RedisError:
            return None
        if not raw:
            return None
        decoded = {k.decode() if isinstance(k, bytes) else k: v for k, v in raw.items()}
        try:
            return {
                "limit": float(decoded.get("limit", 0)),
                "remaining": float(decoded.get("remaining", 0)),
                "reset": float(decoded.get("reset", 0)),
            }
        except (TypeError, ValueError):
            return None

    def before_request(self) -> None:
        state = self.state()
        if not state:
            return
        now = self.clock()
        if state["remaining"] > RESERVE_POINTS or state["reset"] <= now:
            return
        wait = state["reset"] - now + 0.5
        if wait > MAX_INLINE_WAIT_SECONDS:
            raise TwitchRateLimitedError(wait)
        logger.info("Twitch-Kontingent fast erschöpft – warte", extra={"seconds": round(wait, 1)})
        self.sleep(wait)

    def update_from_headers(self, headers: Mapping[str, str]) -> None:
        limit = headers.get("ratelimit-limit") or headers.get("Ratelimit-Limit")
        remaining = headers.get("ratelimit-remaining") or headers.get("Ratelimit-Remaining")
        reset = headers.get("ratelimit-reset") or headers.get("Ratelimit-Reset")
        if remaining is None or reset is None:
            return
        try:
            mapping = {"remaining": int(remaining), "reset": int(reset)}
            if limit is not None:
                mapping["limit"] = int(limit)
            pipe = self.redis.pipeline()
            pipe.hset(self.key, mapping=mapping)
            pipe.expire(self.key, 120)
            pipe.execute()
        except (ValueError, RedisError):
            logger.debug("Rate-Limit-Header nicht verarbeitbar", exc_info=True)

    def seconds_until_reset(self, headers: Mapping[str, str]) -> float:
        reset = headers.get("ratelimit-reset") or headers.get("Ratelimit-Reset")
        try:
            return max(1.0, float(reset) - self.clock() + 0.5) if reset else 60.0
        except ValueError:
            return 60.0
