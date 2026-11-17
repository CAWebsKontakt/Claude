"""Initiales Schema: Benutzer, Streamer, Rechte, Tokens, Clips, Highlights, Warteschlange, Protokoll.

Hinweis: Migrationen sind bewusst unabhängig vom Anwendungscode (keine app.*-Typen),
damit sie auch nach späteren Code-Änderungen reproduzierbar bleiben.
Verschlüsselte Spalten (oauth_tokens.access_token/refresh_token) sind in der DB normales TEXT;
die Verschlüsselung erfolgt in der Anwendung (app.security.crypto.EncryptedText).

Revision ID: 0001
Revises:
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB auf PostgreSQL, JSON auf SQLite (Tests)
JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
# BIGINT-Primärschlüssel; SQLite benötigt INTEGER für Autoincrement
BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "processing_keys",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_processing_keys")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
    )
    op.create_table(
        "streamers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("twitch_login", sa.String(length=32), nullable=False),
        sa.Column("twitch_user_id", sa.String(length=32), nullable=True),
        sa.Column("display_name", sa.String(length=64), nullable=True),
        sa.Column("profile_image_url", sa.String(length=512), nullable=True),
        sa.Column("broadcaster_language", sa.String(length=8), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_live", sa.Boolean(), nullable=False),
        sa.Column("live_title", sa.String(length=300), nullable=True),
        sa.Column("live_game_name", sa.String(length=200), nullable=True),
        sa.Column("live_viewer_count", sa.Integer(), nullable=True),
        sa.Column("live_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("live_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("added_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["added_by_user_id"],
            ["users.id"],
            name=op.f("fk_streamers_added_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_streamers")),
        sa.UniqueConstraint("twitch_login", name=op.f("uq_streamers_twitch_login")),
        sa.UniqueConstraint("twitch_user_id", name=op.f("uq_streamers_twitch_user_id")),
    )
    op.create_table(
        "tiktok_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("open_id", sa.String(length=128), nullable=False),
        sa.Column("union_id", sa.String(length=128), nullable=True),
        sa.Column("username", sa.String(length=128), nullable=True),
        sa.Column("display_name", sa.String(length=128), nullable=True),
        sa.Column("avatar_url", sa.String(length=1024), nullable=True),
        sa.Column("creator_info", JSON_TYPE, nullable=True),
        sa.Column("creator_info_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("connected_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["connected_by_user_id"],
            ["users.id"],
            name=op.f("fk_tiktok_accounts_connected_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tiktok_accounts")),
        sa.UniqueConstraint("open_id", name=op.f("uq_tiktok_accounts_open_id")),
    )
    op.create_table(
        "activity_log",
        sa.Column("id", BIGINT_PK, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", JSON_TYPE, nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("streamer_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["streamer_id"],
            ["streamers.id"],
            name=op.f("fk_activity_log_streamer_id_streamers"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_activity_log_user_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_activity_log")),
    )
    op.create_index("ix_activity_log_created_at", "activity_log", ["created_at"], unique=False)
    op.create_index("ix_activity_log_level_created", "activity_log", ["level", "created_at"], unique=False)
    op.create_table(
        "eventsub_subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("streamer_id", sa.Integer(), nullable=False),
        sa.Column("twitch_subscription_id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("cost", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["streamer_id"],
            ["streamers.id"],
            name=op.f("fk_eventsub_subscriptions_streamer_id_streamers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eventsub_subscriptions")),
        sa.UniqueConstraint(
            "twitch_subscription_id", name=op.f("uq_eventsub_subscriptions_twitch_subscription_id")
        ),
    )
    op.create_index(
        op.f("ix_eventsub_subscriptions_streamer_id"), "eventsub_subscriptions", ["streamer_id"], unique=False
    )
    op.create_table(
        "oauth_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("streamer_id", sa.Integer(), nullable=True),
        sa.Column("tiktok_account_id", sa.Integer(), nullable=True),
        sa.Column("external_user_id", sa.String(length=128), nullable=False),
        sa.Column("external_login", sa.String(length=128), nullable=True),
        sa.Column("scopes", JSON_TYPE, nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_refresh_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["streamer_id"],
            ["streamers.id"],
            name=op.f("fk_oauth_tokens_streamer_id_streamers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tiktok_account_id"],
            ["tiktok_accounts.id"],
            name=op.f("fk_oauth_tokens_tiktok_account_id_tiktok_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_tokens")),
        sa.UniqueConstraint("provider", "external_user_id", name="uq_oauth_tokens_provider_user"),
    )
    op.create_index(op.f("ix_oauth_tokens_streamer_id"), "oauth_tokens", ["streamer_id"], unique=False)
    op.create_index(
        op.f("ix_oauth_tokens_tiktok_account_id"), "oauth_tokens", ["tiktok_account_id"], unique=False
    )
    op.create_table(
        "stream_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("streamer_id", sa.Integer(), nullable=False),
        sa.Column("twitch_stream_id", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column("game_name", sa.String(length=200), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("vod_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("peak_viewers", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["streamer_id"],
            ["streamers.id"],
            name=op.f("fk_stream_sessions_streamer_id_streamers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stream_sessions")),
        sa.UniqueConstraint("twitch_stream_id", name=op.f("uq_stream_sessions_twitch_stream_id")),
    )
    op.create_index(op.f("ix_stream_sessions_streamer_id"), "stream_sessions", ["streamer_id"], unique=False)
    op.create_table(
        "streamer_permissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("streamer_id", sa.Integer(), nullable=False),
        sa.Column("granted_by_name", sa.String(length=200), nullable=False),
        sa.Column("granted_by_role", sa.String(length=32), nullable=False),
        sa.Column("contact", sa.String(length=300), nullable=True),
        sa.Column("allows_processing", sa.Boolean(), nullable=False),
        sa.Column("allows_publishing", sa.Boolean(), nullable=False),
        sa.Column("evidence_reference", sa.Text(), nullable=False),
        sa.Column("scope_note", sa.Text(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.Column("recorded_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["recorded_by_user_id"],
            ["users.id"],
            name=op.f("fk_streamer_permissions_recorded_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["streamer_id"],
            ["streamers.id"],
            name=op.f("fk_streamer_permissions_streamer_id_streamers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_streamer_permissions")),
    )
    op.create_index(
        op.f("ix_streamer_permissions_streamer_id"), "streamer_permissions", ["streamer_id"], unique=False
    )
    op.create_table(
        "twitch_clips",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("streamer_id", sa.Integer(), nullable=False),
        sa.Column("twitch_clip_id", sa.String(length=128), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("embed_url", sa.String(length=512), nullable=True),
        sa.Column("thumbnail_url", sa.String(length=512), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("creator_name", sa.String(length=64), nullable=True),
        sa.Column("game_id", sa.String(length=32), nullable=True),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=False),
        sa.Column("duration", sa.Float(), nullable=False),
        sa.Column("video_id", sa.String(length=64), nullable=True),
        sa.Column("vod_offset", sa.Integer(), nullable=True),
        sa.Column("is_featured", sa.Boolean(), nullable=False),
        sa.Column("clip_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("discovery_score", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["streamer_id"],
            ["streamers.id"],
            name=op.f("fk_twitch_clips_streamer_id_streamers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_twitch_clips")),
        sa.UniqueConstraint("twitch_clip_id", name=op.f("uq_twitch_clips_twitch_clip_id")),
    )
    op.create_index(op.f("ix_twitch_clips_streamer_id"), "twitch_clips", ["streamer_id"], unique=False)
    op.create_index(
        "ix_twitch_clips_streamer_score", "twitch_clips", ["streamer_id", "discovery_score"], unique=False
    )
    op.create_table(
        "chat_activity",
        sa.Column("id", BIGINT_PK, nullable=False),
        sa.Column("streamer_id", sa.Integer(), nullable=False),
        sa.Column("stream_session_id", sa.Integer(), nullable=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bucket_seconds", sa.Integer(), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("unique_chatters", sa.Integer(), nullable=False),
        sa.Column("emote_count", sa.Integer(), nullable=False),
        sa.Column("hype_keyword_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["stream_session_id"],
            ["stream_sessions.id"],
            name=op.f("fk_chat_activity_stream_session_id_stream_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["streamer_id"],
            ["streamers.id"],
            name=op.f("fk_chat_activity_streamer_id_streamers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_activity")),
        sa.UniqueConstraint("streamer_id", "bucket_start", name="uq_chat_activity_streamer_bucket"),
    )
    op.create_index(
        op.f("ix_chat_activity_stream_session_id"), "chat_activity", ["stream_session_id"], unique=False
    )
    op.create_index(op.f("ix_chat_activity_streamer_id"), "chat_activity", ["streamer_id"], unique=False)
    op.create_table(
        "highlights",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("streamer_id", sa.Integer(), nullable=False),
        sa.Column("stream_session_id", sa.Integer(), nullable=True),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("source_ref", sa.String(length=256), nullable=False),
        sa.Column("twitch_clip_id", sa.String(length=128), nullable=True),
        sa.Column("source_file_path", sa.String(length=1024), nullable=True),
        sa.Column("start_seconds", sa.Float(), nullable=False),
        sa.Column("end_seconds", sa.Float(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("score_breakdown", JSON_TYPE, nullable=False),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("title", sa.String(length=150), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("hashtags", JSON_TYPE, nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("perceptual_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["stream_session_id"],
            ["stream_sessions.id"],
            name=op.f("fk_highlights_stream_session_id_stream_sessions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["streamer_id"],
            ["streamers.id"],
            name=op.f("fk_highlights_streamer_id_streamers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_highlights")),
        sa.UniqueConstraint("content_hash", name=op.f("uq_highlights_content_hash")),
    )
    op.create_index(op.f("ix_highlights_perceptual_hash"), "highlights", ["perceptual_hash"], unique=False)
    op.create_index("ix_highlights_status_score", "highlights", ["status", "score"], unique=False)
    op.create_index(
        op.f("ix_highlights_stream_session_id"), "highlights", ["stream_session_id"], unique=False
    )
    op.create_index(op.f("ix_highlights_streamer_id"), "highlights", ["streamer_id"], unique=False)
    op.create_table(
        "rendered_videos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("highlight_id", sa.Integer(), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("thumbnail_path", sa.String(length=1024), nullable=True),
        sa.Column("subtitle_path", sa.String(length=1024), nullable=True),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("duration", sa.Float(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("layout", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["highlight_id"],
            ["highlights.id"],
            name=op.f("fk_rendered_videos_highlight_id_highlights"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rendered_videos")),
    )
    op.create_index(
        op.f("ix_rendered_videos_highlight_id"), "rendered_videos", ["highlight_id"], unique=False
    )
    op.create_table(
        "publish_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("highlight_id", sa.Integer(), nullable=False),
        sa.Column("rendered_video_id", sa.Integer(), nullable=True),
        sa.Column("tiktok_account_id", sa.Integer(), nullable=True),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("caption", sa.Text(), nullable=False),
        sa.Column("hashtags", JSON_TYPE, nullable=False),
        sa.Column("privacy_level", sa.String(length=32), nullable=True),
        sa.Column("disable_comment", sa.Boolean(), nullable=False),
        sa.Column("disable_duet", sa.Boolean(), nullable=False),
        sa.Column("disable_stitch", sa.Boolean(), nullable=False),
        sa.Column("brand_content_toggle", sa.Boolean(), nullable=False),
        sa.Column("brand_organic_toggle", sa.Boolean(), nullable=False),
        sa.Column("is_aigc", sa.Boolean(), nullable=False),
        sa.Column("consent_confirmed", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("approved_by_user_id", sa.Integer(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_id", sa.String(length=128), nullable=True),
        sa.Column("tiktok_post_id", sa.String(length=128), nullable=True),
        sa.Column("fail_reason", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["users.id"],
            name=op.f("fk_publish_jobs_approved_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["highlight_id"],
            ["highlights.id"],
            name=op.f("fk_publish_jobs_highlight_id_highlights"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rendered_video_id"],
            ["rendered_videos.id"],
            name=op.f("fk_publish_jobs_rendered_video_id_rendered_videos"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tiktok_account_id"],
            ["tiktok_accounts.id"],
            name=op.f("fk_publish_jobs_tiktok_account_id_tiktok_accounts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publish_jobs")),
        sa.UniqueConstraint("publish_id", name=op.f("uq_publish_jobs_publish_id")),
    )
    op.create_index(op.f("ix_publish_jobs_highlight_id"), "publish_jobs", ["highlight_id"], unique=False)
    op.create_index(
        op.f("ix_publish_jobs_rendered_video_id"), "publish_jobs", ["rendered_video_id"], unique=False
    )
    op.create_index("ix_publish_jobs_status", "publish_jobs", ["status"], unique=False)
    op.create_index(
        op.f("ix_publish_jobs_tiktok_account_id"), "publish_jobs", ["tiktok_account_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_publish_jobs_tiktok_account_id"), table_name="publish_jobs")
    op.drop_index("ix_publish_jobs_status", table_name="publish_jobs")
    op.drop_index(op.f("ix_publish_jobs_rendered_video_id"), table_name="publish_jobs")
    op.drop_index(op.f("ix_publish_jobs_highlight_id"), table_name="publish_jobs")
    op.drop_table("publish_jobs")
    op.drop_index(op.f("ix_rendered_videos_highlight_id"), table_name="rendered_videos")
    op.drop_table("rendered_videos")
    op.drop_index(op.f("ix_highlights_streamer_id"), table_name="highlights")
    op.drop_index(op.f("ix_highlights_stream_session_id"), table_name="highlights")
    op.drop_index("ix_highlights_status_score", table_name="highlights")
    op.drop_index(op.f("ix_highlights_perceptual_hash"), table_name="highlights")
    op.drop_table("highlights")
    op.drop_index(op.f("ix_chat_activity_streamer_id"), table_name="chat_activity")
    op.drop_index(op.f("ix_chat_activity_stream_session_id"), table_name="chat_activity")
    op.drop_table("chat_activity")
    op.drop_index("ix_twitch_clips_streamer_score", table_name="twitch_clips")
    op.drop_index(op.f("ix_twitch_clips_streamer_id"), table_name="twitch_clips")
    op.drop_table("twitch_clips")
    op.drop_index(op.f("ix_streamer_permissions_streamer_id"), table_name="streamer_permissions")
    op.drop_table("streamer_permissions")
    op.drop_index(op.f("ix_stream_sessions_streamer_id"), table_name="stream_sessions")
    op.drop_table("stream_sessions")
    op.drop_index(op.f("ix_oauth_tokens_tiktok_account_id"), table_name="oauth_tokens")
    op.drop_index(op.f("ix_oauth_tokens_streamer_id"), table_name="oauth_tokens")
    op.drop_table("oauth_tokens")
    op.drop_index(op.f("ix_eventsub_subscriptions_streamer_id"), table_name="eventsub_subscriptions")
    op.drop_table("eventsub_subscriptions")
    op.drop_index("ix_activity_log_level_created", table_name="activity_log")
    op.drop_index("ix_activity_log_created_at", table_name="activity_log")
    op.drop_table("activity_log")
    op.drop_table("tiktok_accounts")
    op.drop_table("streamers")
    op.drop_table("users")
    op.drop_table("processing_keys")
