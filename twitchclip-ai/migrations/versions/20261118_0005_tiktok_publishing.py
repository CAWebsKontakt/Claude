"""Phase 6: TikTok-Veröffentlichung – Rohstatus, Upload-Fortschritt, Titelbild-Zeitpunkt, Export-Datei.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("publish_jobs") as batch:
        batch.add_column(sa.Column("tiktok_status", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("uploaded_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("video_cover_timestamp_ms", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("export_path", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("publish_jobs") as batch:
        batch.drop_column("export_path")
        batch.drop_column("video_cover_timestamp_ms")
        batch.drop_column("uploaded_bytes")
        batch.drop_column("tiktok_status")
