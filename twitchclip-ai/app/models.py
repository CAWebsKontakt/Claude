"""Datenmodell von TwitchClip AI.

Übersicht:
- users                    Dashboard-Benutzer mit Rollen
- streamers                überwachte Twitch-Kanäle (Modus A öffentlich / Modus B autorisiert)
- streamer_permissions     dokumentierte Erlaubnis zur Weiterverarbeitung/Veröffentlichung (Modus B)
- oauth_tokens             verschlüsselte OAuth-Tokens (Twitch-Streamer/Editor, TikTok-Konten)
- tiktok_accounts          verbundene TikTok-Konten inkl. Creator-Info
- eventsub_subscriptions   Twitch-EventSub-Abos (stream.online/offline)
- stream_sessions          einzelne Livestreams
- twitch_clips             öffentliche Clips (Modus A: nur Metadaten + Bewertung)
- chat_activity            aggregierte Chat-Aktivität (keine Nachrichteninhalte)
- highlights               KI-erkannte Highlight-Kandidaten
- rendered_videos          fertig geschnittene 1080x1920-Videos
- publish_jobs             TikTok-Warteschlange inkl. Freigaben und Status
- processing_keys          Idempotenz-Schlüssel gegen doppelte Verarbeitung
- activity_log             Aktivitäts- und Fehlerprotokoll
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, JSONType, UTCDateTime
from app.enums import (
    ActivityLevel,
    ClipStatus,
    GrantorRole,
    HighlightStatus,
    PublishMethod,
    PublishStatus,
    StreamerMode,
    StreamStatus,
    UserRole,
)
from app.security.crypto import EncryptedText
from app.timeutil import utcnow


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow, server_default=func.now(), nullable=False
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default=UserRole.VIEWER.value, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Wird bei Passwortänderung/Abmelden-überall erhöht -> alte Sessions werden ungültig.
    session_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    @property
    def can_edit(self) -> bool:
        return self.role in (UserRole.ADMIN, UserRole.EDITOR)


class Streamer(TimestampMixin, Base):
    __tablename__ = "streamers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    twitch_login: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    twitch_user_id: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile_image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    broadcaster_language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), default=StreamerMode.PUBLIC.value, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Demo-Kanal für den Testmodus (eigene Beispieldateien, kein Twitch-Konto)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)

    # Live-Status (EventSub + Polling als Rückfallebene)
    is_live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    live_title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    live_game_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    live_viewer_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    live_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    live_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Letzte erfolgreiche Suche nach öffentlichen Clips
    last_clip_scan_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    permissions: Mapped[list[StreamerPermission]] = relationship(
        back_populates="streamer",
        cascade="all, delete-orphan",
        order_by="StreamerPermission.created_at.desc()",
    )
    oauth_tokens: Mapped[list[OAuthToken]] = relationship(
        back_populates="streamer", cascade="all, delete-orphan"
    )
    eventsub_subscriptions: Mapped[list[EventSubSubscription]] = relationship(
        back_populates="streamer", cascade="all, delete-orphan"
    )

    @property
    def name(self) -> str:
        return self.display_name or self.twitch_login

    @property
    def channel_url(self) -> str:
        return f"https://www.twitch.tv/{self.twitch_login}"


class StreamerPermission(Base):
    """Nachweis, dass Streamer oder berechtigter Editor der Verarbeitung zugestimmt haben."""

    __tablename__ = "streamer_permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    streamer_id: Mapped[int] = mapped_column(
        ForeignKey("streamers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    granted_by_name: Mapped[str] = mapped_column(String(200), nullable=False)
    granted_by_role: Mapped[str] = mapped_column(
        String(32), default=GrantorRole.BROADCASTER.value, nullable=False
    )
    contact: Mapped[str | None] = mapped_column(String(300), nullable=True)
    allows_processing: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allows_publishing: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Wo liegt der Nachweis? (z. B. "Vertrag vom 01.11.2026, Ablage /Verträge/streamer.pdf")
    evidence_reference: Mapped[str] = mapped_column(Text, nullable=False)
    scope_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, server_default=func.now(), nullable=False
    )

    streamer: Mapped[Streamer] = relationship(back_populates="permissions")
    recorded_by: Mapped[User | None] = relationship()

    def is_valid_at(self, moment: datetime) -> bool:
        if self.revoked_at is not None:
            return False
        if self.valid_from > moment:
            return False
        return not (self.valid_until is not None and self.valid_until < moment)


class TikTokAccount(TimestampMixin, Base):
    __tablename__ = "tiktok_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    open_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    union_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    creator_info: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    creator_info_fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    connected_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    oauth_tokens: Mapped[list[OAuthToken]] = relationship(
        back_populates="tiktok_account", cascade="all, delete-orphan"
    )


class OAuthToken(TimestampMixin, Base):
    """OAuth-Tokens. access_token/refresh_token liegen ausschließlich verschlüsselt in der DB."""

    __tablename__ = "oauth_tokens"
    __table_args__ = (
        # Twitch: ein Token je (Streamer, autorisierender Nutzer) – ein Editor kann mehrere Kanäle betreuen.
        Index(
            "uq_oauth_tokens_streamer_user",
            "provider",
            "streamer_id",
            "external_user_id",
            unique=True,
            postgresql_where=text("streamer_id IS NOT NULL"),
            sqlite_where=text("streamer_id IS NOT NULL"),
        ),
        # TikTok: ein Token je Konto
        Index(
            "uq_oauth_tokens_account_user",
            "provider",
            "external_user_id",
            unique=True,
            postgresql_where=text("streamer_id IS NULL"),
            sqlite_where=text("streamer_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    streamer_id: Mapped[int | None] = mapped_column(
        ForeignKey("streamers.id", ondelete="CASCADE"), nullable=True, index=True
    )
    tiktok_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("tiktok_accounts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    external_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_login: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scopes: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    access_token: Mapped[str] = mapped_column(EncryptedText(), nullable=False)
    refresh_token: Mapped[str | None] = mapped_column(EncryptedText(), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_refresh_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Twitch: broadcaster | editor
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_validated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    streamer: Mapped[Streamer | None] = relationship(back_populates="oauth_tokens")
    tiktok_account: Mapped[TikTokAccount | None] = relationship(back_populates="oauth_tokens")

    def has_any_scope(self, required: set[str] | frozenset[str]) -> bool:
        return bool(set(self.scopes or []) & set(required))


class EventSubSubscription(TimestampMixin, Base):
    __tablename__ = "eventsub_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    streamer_id: Mapped[int] = mapped_column(
        ForeignKey("streamers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    twitch_subscription_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(8), default="1", nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    cost: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    streamer: Mapped[Streamer] = relationship(back_populates="eventsub_subscriptions")


class StreamSession(TimestampMixin, Base):
    __tablename__ = "stream_sessions"
    __table_args__ = (Index("ix_stream_sessions_status", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    streamer_id: Mapped[int] = mapped_column(
        ForeignKey("streamers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    twitch_stream_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    game_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    vod_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=StreamStatus.LIVE.value, nullable=False)
    peak_viewers: Mapped[int | None] = mapped_column(Integer, nullable=True)

    streamer: Mapped[Streamer] = relationship()


class TwitchClip(Base):
    """Öffentliche Clips (Modus A). Es werden nur Metadaten gespeichert, keine Videodateien."""

    __tablename__ = "twitch_clips"
    __table_args__ = (
        Index("ix_twitch_clips_streamer_score", "streamer_id", "discovery_score"),
        Index("ix_twitch_clips_clip_created_at", "clip_created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    streamer_id: Mapped[int] = mapped_column(
        ForeignKey("streamers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    twitch_clip_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    embed_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    creator_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    game_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    video_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vod_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    clip_created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    discovery_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    score_breakdown: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=ClipStatus.NEW.value, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, server_default=func.now(), nullable=False
    )

    streamer: Mapped[Streamer] = relationship()


class ChatActivity(Base):
    """Chat-Aktivität in Zeitfenstern. Es werden bewusst keine Nachrichteninhalte gespeichert."""

    __tablename__ = "chat_activity"
    __table_args__ = (
        UniqueConstraint("streamer_id", "bucket_start", name="uq_chat_activity_streamer_bucket"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    streamer_id: Mapped[int] = mapped_column(
        ForeignKey("streamers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stream_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("stream_sessions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    bucket_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    bucket_seconds: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unique_chatters: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    emote_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    hype_keyword_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ClipRequest(TimestampMixin, Base):
    """Ein berechtigter Clip, der erzeugt und/oder heruntergeladen wird (Modus B oder Testmodus).

    dedup_key verhindert doppelte Verarbeitung (z. B. "viewer:<clip_id>", "vod:<vod>:<ende>:<dauer>").
    """

    __tablename__ = "clip_requests"
    __table_args__ = (Index("ix_clip_requests_status", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    streamer_id: Mapped[int] = mapped_column(
        ForeignKey("streamers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stream_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("stream_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    oauth_token_id: Mapped[int | None] = mapped_column(
        ForeignKey("oauth_tokens.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    vod_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Twitch-Konvention: vod_offset = ENDE des Clips im VOD (Sekunden)
    vod_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    twitch_clip_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String(140), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="planned", nullable=False)
    reason: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    media_info: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    analyzed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    requested_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    streamer: Mapped[Streamer] = relationship()
    stream_session: Mapped[StreamSession | None] = relationship()

    @property
    def start_seconds(self) -> int | None:
        if self.vod_offset is None or self.duration is None:
            return None
        return max(0, self.vod_offset - self.duration)


class Highlight(TimestampMixin, Base):
    __tablename__ = "highlights"
    __table_args__ = (Index("ix_highlights_status_score", "status", "score"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    streamer_id: Mapped[int] = mapped_column(
        ForeignKey("streamers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stream_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("stream_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    clip_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("clip_requests.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    twitch_clip_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    start_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    end_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    score_breakdown: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    title: Mapped[str | None] = mapped_column(String(150), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    hashtags: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    # Duplikaterkennung: exakter Inhalts-Hash + perzeptueller Hash
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    perceptual_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Gruppe für die Auswahl der besten 3–5 (z. B. "session-12" oder "samples-3-2026-11-18")
    selection_group: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Analyse-Details für Schnitt/Untertitel: Facecam-Box, Wörter mit Zeitstempeln, Signalverläufe
    analysis: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=HighlightStatus.CANDIDATE.value, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    streamer: Mapped[Streamer] = relationship()
    stream_session: Mapped[StreamSession | None] = relationship()
    rendered_videos: Mapped[list[RenderedVideo]] = relationship(
        back_populates="highlight", cascade="all, delete-orphan", order_by="RenderedVideo.created_at.desc()"
    )

    @property
    def duration(self) -> float:
        return max(0.0, self.end_seconds - self.start_seconds)

    @property
    def current_video(self) -> RenderedVideo | None:
        for video in self.rendered_videos:
            if video.deleted_at is None:
                return video
        return None


class RenderedVideo(Base):
    __tablename__ = "rendered_videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    highlight_id: Mapped[int] = mapped_column(
        ForeignKey("highlights.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    thumbnail_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    subtitle_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    width: Mapped[int] = mapped_column(Integer, default=1080, nullable=False)
    height: Mapped[int] = mapped_column(Integer, default=1920, nullable=False)
    duration: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    layout: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, server_default=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    highlight: Mapped[Highlight] = relationship(back_populates="rendered_videos")


class PublishJob(TimestampMixin, Base):
    """TikTok-Warteschlange. Ohne explizite Freigabe (approved_at) wird nichts hochgeladen."""

    __tablename__ = "publish_jobs"
    __table_args__ = (Index("ix_publish_jobs_status", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    highlight_id: Mapped[int] = mapped_column(
        ForeignKey("highlights.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rendered_video_id: Mapped[int | None] = mapped_column(
        ForeignKey("rendered_videos.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tiktok_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("tiktok_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    method: Mapped[str] = mapped_column(String(16), default=PublishMethod.MANUAL_EXPORT.value, nullable=False)
    caption: Mapped[str] = mapped_column(Text, default="", nullable=False)
    hashtags: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    # Pflichtentscheidungen des Nutzers (TikTok UX-Richtlinien) – kein Default für privacy_level!
    privacy_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    disable_comment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    disable_duet: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    disable_stitch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    brand_content_toggle: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    brand_organic_toggle: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_aigc: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    consent_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=PublishStatus.PENDING_REVIEW.value, nullable=False
    )
    approved_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    publish_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    tiktok_post_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fail_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    highlight: Mapped[Highlight] = relationship()
    rendered_video: Mapped[RenderedVideo | None] = relationship()
    tiktok_account: Mapped[TikTokAccount | None] = relationship()
    approved_by: Mapped[User | None] = relationship()


class ProcessingKey(Base):
    """Idempotenz: Ein Schlüssel (z. B. 'vod:123:4500') wird genau einmal verarbeitet."""

    __tablename__ = "processing_keys"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, server_default=func.now(), nullable=False
    )


class ActivityLog(Base):
    __tablename__ = "activity_log"
    __table_args__ = (
        Index("ix_activity_log_created_at", "created_at"),
        Index("ix_activity_log_level_created", "level", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, server_default=func.now(), nullable=False
    )
    level: Mapped[str] = mapped_column(String(16), default=ActivityLevel.INFO.value, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    streamer_id: Mapped[int | None] = mapped_column(
        ForeignKey("streamers.id", ondelete="SET NULL"), nullable=True
    )

    user: Mapped[User | None] = relationship()
    streamer: Mapped[Streamer | None] = relationship()
