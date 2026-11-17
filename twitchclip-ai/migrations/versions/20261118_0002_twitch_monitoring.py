"""Phase 2: Twitch-Überwachung – Zeitpunkt der letzten Clip-Suche und Bewertungsdetails je Clip.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("streamers") as batch:
        batch.add_column(sa.Column("last_clip_scan_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("twitch_clips") as batch:
        batch.add_column(sa.Column("score_breakdown", JSON_TYPE, nullable=True))
    op.create_index("ix_twitch_clips_clip_created_at", "twitch_clips", ["clip_created_at"], unique=False)
    op.create_index("ix_stream_sessions_status", "stream_sessions", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_stream_sessions_status", table_name="stream_sessions")
    op.drop_index("ix_twitch_clips_clip_created_at", table_name="twitch_clips")
    with op.batch_alter_table("twitch_clips") as batch:
        batch.drop_column("score_breakdown")
    with op.batch_alter_table("streamers") as batch:
        batch.drop_column("last_clip_scan_at")
