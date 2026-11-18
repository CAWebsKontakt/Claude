"""Zentrale Konfiguration. Alle Werte kommen aus Umgebungsvariablen bzw. der .env-Datei."""

from __future__ import annotations

import base64
import binascii
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Platzhalter aus .env.example – dürfen in Produktion niemals verwendet werden.
PLACEHOLDER_MARKERS = ("CHANGE_ME", "change-me", "changeme")


def _is_valid_fernet_key(key: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(key.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return False
    return len(raw) == 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Allgemein -------------------------------------------------------
    app_env: Literal["production", "development", "test"] = "production"
    app_name: str = "TwitchClip AI"
    app_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"
    timezone: str = "Europe/Berlin"

    # --- Sicherheit ------------------------------------------------------
    secret_key: SecretStr = Field(default=SecretStr(""))
    # Kommagetrennte Fernet-Schlüssel. Der erste verschlüsselt, alle entschlüsseln (Schlüsselrotation).
    token_encryption_key: SecretStr = Field(default=SecretStr(""))
    session_max_age_hours: int = Field(default=12, ge=1, le=24 * 30)
    session_cookie_secure: bool = True
    login_max_attempts: int = Field(default=10, ge=3, le=100)
    login_window_seconds: int = Field(default=900, ge=60)

    # --- Infrastruktur ---------------------------------------------------
    database_url: str = "postgresql+psycopg://twitchclip:twitchclip@postgres:5432/twitchclip"
    redis_url: str = "redis://redis:6379/0"
    data_dir: Path = Path("/data")

    # --- Fachliche Konfiguration (aus dem Lastenheft) -------------------
    streamer_list: Annotated[list[str], NoDecode] = Field(default_factory=list)
    max_clips_per_day: int = Field(default=10, ge=0, le=500)
    min_highlight_score: int = Field(default=60, ge=0, le=100)
    clip_language: Literal["de", "en", "auto"] = "de"
    clip_duration_min: int = Field(default=20, ge=5, le=60)
    clip_duration_max: int = Field(default=45, ge=5, le=60)
    highlights_per_stream_min: int = Field(default=3, ge=1, le=20)
    highlights_per_stream_max: int = Field(default=5, ge=1, le=20)
    storage_retention_days: int = Field(default=7, ge=1, le=365)
    storage_max_gb: float = Field(default=20.0, gt=0)
    activity_retention_days: int = Field(default=90, ge=7, le=3650)

    # --- Twitch ----------------------------------------------------------
    twitch_client_id: str = ""
    twitch_client_secret: SecretStr = Field(default=SecretStr(""))
    twitch_eventsub_secret: SecretStr = Field(default=SecretStr(""))
    # EventSub-Webhooks (benötigen HTTPS auf Port 443). false = nur Polling.
    twitch_eventsub_enabled: bool = True
    # Rückfallebene/Abgleich des Live-Status per Get Streams
    twitch_poll_interval_seconds: int = Field(default=180, ge=60, le=3600)
    # Suche nach öffentlichen Clips
    clip_discovery_interval_minutes: int = Field(default=30, ge=5, le=1440)
    clip_discovery_lookback_hours: int = Field(default=48, ge=1, le=24 * 30)
    clip_retention_days: int = Field(default=30, ge=1, le=365)
    # Für Tests/Mocks überschreibbar
    twitch_api_base: str = "https://api.twitch.tv/helix"
    twitch_auth_base: str = "https://id.twitch.tv/oauth2"
    # Pfade der (neueren) Clip-Endpunkte – bei Abweichung zur offiziellen Referenz hier anpassbar
    twitch_vod_clip_path: str = "/videos/clips"
    twitch_clip_download_path: str = "/clips/downloads"
    # Gültigkeit von Autorisierungslinks für Streamer/Editoren
    twitch_invite_valid_days: int = Field(default=7, ge=1, le=60)
    # Höchstzahl automatisch geplanter Clip-Kandidaten je Stream (begrenzt auch neue Clips im Kanal)
    clip_candidates_per_stream: int = Field(default=8, ge=1, le=30)
    # Größte erlaubte Download-Datei in MB
    clip_download_max_mb: int = Field(default=500, ge=10, le=4000)

    # --- TikTok ----------------------------------------------------------
    tiktok_client_key: str = ""
    tiktok_client_secret: SecretStr = Field(default=SecretStr(""))
    # Erst auf true setzen, wenn TikTok die App für Direct Post freigegeben (auditiert) hat.
    tiktok_direct_post_enabled: bool = False

    # --- KI / Medien (ab Phase 4) ----------------------------------------
    whisper_model: str = "small"
    whisper_device: Literal["cpu", "cuda", "auto"] = "cpu"
    whisper_compute_type: str = "int8"
    # Spracherkennung abschaltbar (z. B. sehr kleiner VPS); dann zählen nur Ton, Bild und Chat
    analysis_transcription: bool = True
    # Chat-Statistik (nur Zählwerte) für Streamer, die die Chat-Scopes erlaubt haben
    chat_stats_enabled: bool = True
    # Ab diesem Vielfachen des Medians gilt eine Chat-Spitze als Highlight-Kandidat
    chat_spike_factor: float = Field(default=2.5, ge=1.2, le=20)
    # Ähnlichkeitsgrenze für Duplikate (Hamming-Abstand der Bild-Hashes, 0–64)
    duplicate_hash_distance: int = Field(default=10, ge=0, le=32)

    # --- Testmodus -------------------------------------------------------
    # Verarbeitet nur lokale Beispieldateien, ruft keine externen APIs auf.
    test_mode: bool = False

    # ------------------------------------------------------------------
    @field_validator("streamer_list", mode="before")
    @classmethod
    def _split_streamer_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            items = value.replace(";", ",").replace("\n", ",").split(",")
        elif isinstance(value, list | tuple | set):
            items = [str(v) for v in value]
        else:
            raise ValueError("STREAMER_LIST muss eine kommagetrennte Liste sein")
        seen: list[str] = []
        for item in items:
            login = item.strip().lstrip("@").lower()
            if login and login not in seen:
                seen.append(login)
        return seen

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unbekannte Zeitzone: {value}") from exc
        return value

    @field_validator("app_base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("APP_BASE_URL muss eine absolute http(s)-URL sein")
        return value.rstrip("/")

    @field_validator("twitch_eventsub_secret")
    @classmethod
    def _validate_eventsub_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if raw and not (10 <= len(raw) <= 100):
            raise ValueError("TWITCH_EVENTSUB_SECRET muss 10–100 Zeichen lang sein (Vorgabe von Twitch)")
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL ungültig")
        return value

    @model_validator(mode="after")
    def _validate_consistency(self) -> Settings:
        if self.clip_duration_min > self.clip_duration_max:
            raise ValueError("CLIP_DURATION_MIN darf nicht größer als CLIP_DURATION_MAX sein")
        if self.highlights_per_stream_min > self.highlights_per_stream_max:
            raise ValueError("HIGHLIGHTS_PER_STREAM_MIN darf nicht größer als HIGHLIGHTS_PER_STREAM_MAX sein")
        if self.app_env == "production":
            self._validate_production_secrets()
        return self

    def _validate_production_secrets(self) -> None:
        secret = self.secret_key.get_secret_value()
        if len(secret) < 32 or any(m in secret for m in PLACEHOLDER_MARKERS):
            raise ValueError("SECRET_KEY fehlt oder ist zu kurz (mind. 32 Zeichen, kein Platzhalter)")
        keys = self.encryption_keys
        if not keys or not all(_is_valid_fernet_key(k) for k in keys):
            raise ValueError(
                "TOKEN_ENCRYPTION_KEY fehlt oder ist kein gültiger Fernet-Schlüssel "
                "(erzeugen mit: python -m app.cli generate-secrets)"
            )
        if not self.app_base_url.startswith("https://"):
            raise ValueError("APP_BASE_URL muss in Produktion mit https:// beginnen")

    # --- Abgeleitete Werte -------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def encryption_keys(self) -> list[str]:
        raw = self.token_encryption_key.get_secret_value()
        return [k.strip() for k in raw.split(",") if k.strip()]

    @property
    def public_host(self) -> str:
        return urlparse(self.app_base_url).hostname or "localhost"

    @property
    def twitch_redirect_uri(self) -> str:
        return f"{self.app_base_url}/oauth/twitch/callback"

    @property
    def tiktok_redirect_uri(self) -> str:
        return f"{self.app_base_url}/oauth/tiktok/callback"

    @property
    def eventsub_callback_url(self) -> str:
        return f"{self.app_base_url}/webhooks/twitch/eventsub"

    @property
    def twitch_configured(self) -> bool:
        return bool(self.twitch_client_id and self.twitch_client_secret.get_secret_value())

    @property
    def eventsub_webhooks_active(self) -> bool:
        """EventSub-Webhooks nur mit öffentlicher HTTPS-Adresse (Port 443) und gesetztem Secret."""
        host = self.public_host
        return (
            self.twitch_eventsub_enabled
            and self.twitch_configured
            and bool(self.twitch_eventsub_secret.get_secret_value())
            and self.app_base_url.startswith("https://")
            and host not in ("localhost", "127.0.0.1", "testserver")
            and ":" not in self.app_base_url.split("//", 1)[1].split("/", 1)[0]
        )

    @property
    def tiktok_configured(self) -> bool:
        return bool(self.tiktok_client_key and self.tiktok_client_secret.get_secret_value())

    # Verzeichnisse unterhalb von DATA_DIR
    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def renders_dir(self) -> Path:
        return self.data_dir / "renders"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def samples_dir(self) -> Path:
        return self.data_dir / "samples"

    @property
    def models_dir(self) -> Path:
        """Whisper-Modelle (Cache, wird nicht automatisch gelöscht)."""
        return self.data_dir / "models"

    def managed_dirs(self) -> list[Path]:
        return [self.tmp_dir, self.downloads_dir, self.renders_dir, self.exports_dir, self.samples_dir]

    def public_summary(self) -> dict[str, object]:
        """Konfiguration ohne Geheimnisse – für Dashboard und CLI."""
        return {
            "APP_ENV": self.app_env,
            "APP_BASE_URL": self.app_base_url,
            "TIMEZONE": self.timezone,
            "STREAMER_LIST": ", ".join(self.streamer_list) or "–",
            "MAX_CLIPS_PER_DAY": self.max_clips_per_day,
            "MIN_HIGHLIGHT_SCORE": self.min_highlight_score,
            "CLIP_LANGUAGE": self.clip_language,
            "CLIP_DURATION_MIN": self.clip_duration_min,
            "CLIP_DURATION_MAX": self.clip_duration_max,
            "HIGHLIGHTS_PER_STREAM": f"{self.highlights_per_stream_min}–{self.highlights_per_stream_max}",
            "STORAGE_RETENTION_DAYS": self.storage_retention_days,
            "STORAGE_MAX_GB": self.storage_max_gb,
            "ACTIVITY_RETENTION_DAYS": self.activity_retention_days,
            "DATA_DIR": str(self.data_dir),
            "TWITCH_CLIENT_ID": "gesetzt" if self.twitch_client_id else "nicht gesetzt",
            "TWITCH_CLIENT_SECRET": "gesetzt"
            if self.twitch_client_secret.get_secret_value()
            else "nicht gesetzt",
            "TWITCH_EVENTSUB": "Webhooks aktiv" if self.eventsub_webhooks_active else "aus (nur Polling)",
            "TWITCH_POLL_INTERVAL_SECONDS": self.twitch_poll_interval_seconds,
            "CLIP_DISCOVERY_INTERVAL_MINUTES": self.clip_discovery_interval_minutes,
            "CLIP_DISCOVERY_LOOKBACK_HOURS": self.clip_discovery_lookback_hours,
            "CLIP_RETENTION_DAYS": self.clip_retention_days,
            "CLIP_CANDIDATES_PER_STREAM": self.clip_candidates_per_stream,
            "TWITCH_VOD_CLIP_PATH": self.twitch_vod_clip_path,
            "TWITCH_CLIP_DOWNLOAD_PATH": self.twitch_clip_download_path,
            "TIKTOK_CLIENT_KEY": "gesetzt" if self.tiktok_client_key else "nicht gesetzt",
            "TIKTOK_DIRECT_POST_ENABLED": self.tiktok_direct_post_enabled,
            "WHISPER_MODEL": self.whisper_model,
            "ANALYSIS_TRANSCRIPTION": self.analysis_transcription,
            "CHAT_STATS_ENABLED": self.chat_stats_enabled,
            "CHAT_SPIKE_FACTOR": self.chat_spike_factor,
            "DUPLICATE_HASH_DISTANCE": self.duplicate_hash_distance,
            "TEST_MODE": self.test_mode,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # TCAI_ENV_FILE="" deaktiviert das Lesen einer .env-Datei (z. B. in Tests).
    env_file = os.environ.get("TCAI_ENV_FILE", ".env")
    return Settings(_env_file=env_file or None)


def reset_settings_cache() -> None:
    get_settings.cache_clear()
