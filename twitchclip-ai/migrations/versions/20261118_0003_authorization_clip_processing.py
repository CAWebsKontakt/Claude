"""Phase 3: Twitch-Autorisierung je Streamer/Editor, Clip-Verarbeitung, Demo-Kanal für den Testmodus.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("oauth_tokens") as batch:
        batch.drop_constraint("uq_oauth_tokens_provider_user", type_="unique")
        batch.add_column(sa.Column("role", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "uq_oauth_tokens_streamer_user",
        "oauth_tokens",
        ["provider", "streamer_id", "external_user_id"],
        unique=True,
        postgresql_where=sa.text("streamer_id IS NOT NULL"),
        sqlite_where=sa.text("streamer_id IS NOT NULL"),
    )
    op.create_index(
        "uq_oauth_tokens_account_user",
        "oauth_tokens",
        ["provider", "external_user_id"],
        unique=True,
        postgresql_where=sa.text("streamer_id IS NULL"),
        sqlite_where=sa.text("streamer_id IS NULL"),
    )

    with op.batch_alter_table("streamers") as batch:
        batch.add_column(sa.Column("is_demo", sa.Boolean(), server_default="0", nullable=False))

    op.create_table(
        "clip_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("streamer_id", sa.Integer(), nullable=False),
        sa.Column("stream_session_id", sa.Integer(), nullable=True),
        sa.Column("oauth_token_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("dedup_key", sa.String(length=255), nullable=False),
        sa.Column("vod_id", sa.String(length=64), nullable=True),
        sa.Column("vod_offset", sa.Integer(), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column("twitch_clip_id", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=140), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", JSON_TYPE, nullable=True),
        sa.Column("file_path", sa.String(length=1024), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("media_info", JSON_TYPE, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("requested_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["oauth_token_id"],
            ["oauth_tokens.id"],
            name=op.f("fk_clip_requests_oauth_token_id_oauth_tokens"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            name=op.f("fk_clip_requests_requested_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["stream_session_id"],
            ["stream_sessions.id"],
            name=op.f("fk_clip_requests_stream_session_id_stream_sessions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["streamer_id"],
            ["streamers.id"],
            name=op.f("fk_clip_requests_streamer_id_streamers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clip_requests")),
        sa.UniqueConstraint("dedup_key", name=op.f("uq_clip_requests_dedup_key")),
    )
    op.create_index("ix_clip_requests_status", "clip_requests", ["status"], unique=False)
    op.create_index(op.f("ix_clip_requests_streamer_id"), "clip_requests", ["streamer_id"], unique=False)
    op.create_index(
        op.f("ix_clip_requests_stream_session_id"), "clip_requests", ["stream_session_id"], unique=False
    )
    op.create_index(
        op.f("ix_clip_requests_twitch_clip_id"), "clip_requests", ["twitch_clip_id"], unique=False
    )
    op.create_index(op.f("ix_clip_requests_sha256"), "clip_requests", ["sha256"], unique=False)

    with op.batch_alter_table("highlights") as batch:
        batch.add_column(sa.Column("clip_request_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_highlights_clip_request_id_clip_requests"),
            "clip_requests",
            ["clip_request_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(op.f("ix_highlights_clip_request_id"), ["clip_request_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("highlights") as batch:
        batch.drop_index(op.f("ix_highlights_clip_request_id"))
        batch.drop_constraint(op.f("fk_highlights_clip_request_id_clip_requests"), type_="foreignkey")
        batch.drop_column("clip_request_id")
    op.drop_index(op.f("ix_clip_requests_sha256"), table_name="clip_requests")
    op.drop_index(op.f("ix_clip_requests_twitch_clip_id"), table_name="clip_requests")
    op.drop_index(op.f("ix_clip_requests_stream_session_id"), table_name="clip_requests")
    op.drop_index(op.f("ix_clip_requests_streamer_id"), table_name="clip_requests")
    op.drop_index("ix_clip_requests_status", table_name="clip_requests")
    op.drop_table("clip_requests")
    with op.batch_alter_table("streamers") as batch:
        batch.drop_column("is_demo")
    op.drop_index("uq_oauth_tokens_account_user", table_name="oauth_tokens")
    op.drop_index("uq_oauth_tokens_streamer_user", table_name="oauth_tokens")
    with op.batch_alter_table("oauth_tokens") as batch:
        batch.drop_column("last_validated_at")
        batch.drop_column("role")
        batch.create_unique_constraint("uq_oauth_tokens_provider_user", ["provider", "external_user_id"])
