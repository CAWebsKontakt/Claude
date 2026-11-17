"""App-Access-Token (Client-Credentials-Flow), verschlüsselt in Redis zwischengespeichert."""

from __future__ import annotations

import logging
from contextlib import suppress

import httpx
from redis import Redis
from redis.exceptions import RedisError

from app.config import Settings
from app.integrations.twitch.errors import TwitchAuthError, TwitchNotConfiguredError
from app.security import crypto

logger = logging.getLogger(__name__)

CACHE_KEY = "tcai:twitch:app_token"
# Token etwas vor Ablauf erneuern
EXPIRY_MARGIN_SECONDS = 300


class AppTokenProvider:
    def __init__(self, settings: Settings, redis: Redis, http: httpx.Client) -> None:
        if not settings.twitch_configured:
            raise TwitchNotConfiguredError("TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET sind nicht gesetzt")
        self.settings = settings
        self.redis = redis
        self.http = http

    def get_token(self) -> str:
        cached = self._read_cache()
        if cached:
            return cached
        return self._fetch_new()

    def invalidate(self) -> None:
        with suppress(RedisError):
            self.redis.delete(CACHE_KEY)

    def _read_cache(self) -> str | None:
        try:
            raw = self.redis.get(CACHE_KEY)
        except RedisError:
            return None
        if not raw:
            return None
        try:
            return crypto.decrypt(raw.decode() if isinstance(raw, bytes) else raw)
        except crypto.DecryptionError:
            self.invalidate()
            return None

    def _fetch_new(self) -> str:
        try:
            response = self.http.post(
                f"{self.settings.twitch_auth_base}/token",
                data={
                    "client_id": self.settings.twitch_client_id,
                    "client_secret": self.settings.twitch_client_secret.get_secret_value(),
                    "grant_type": "client_credentials",
                },
                timeout=15,
            )
        except httpx.HTTPError as exc:
            raise TwitchAuthError(
                f"Twitch-Token-Endpunkt nicht erreichbar: {exc.__class__.__name__}"
            ) from exc
        if response.status_code != 200:
            # Antworttext kann Hinweise enthalten, aber nie das Secret
            raise TwitchAuthError(
                f"App-Token abgelehnt (HTTP {response.status_code}): {response.text[:200]} – "
                "TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET prüfen"
            )
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise TwitchAuthError("Antwort enthält kein access_token")
        ttl = max(60, int(payload.get("expires_in", 3600)) - EXPIRY_MARGIN_SECONDS)
        try:
            self.redis.set(CACHE_KEY, crypto.encrypt(token), ex=ttl)
        except RedisError:
            logger.warning("App-Token konnte nicht zwischengespeichert werden")
        logger.info("Neues Twitch-App-Token bezogen", extra={"ttl": ttl})
        return token
