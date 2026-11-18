"""Phase 4: KI-Analyse – Analysezeitpunkt je Clip-Auftrag, Auswahlgruppe und Analysedaten je Highlight.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("clip_requests") as batch:
        batch.add_column(sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("highlights") as batch:
        batch.add_column(sa.Column("selection_group", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("analysis", JSON_TYPE, nullable=True))
        batch.create_index(op.f("ix_highlights_selection_group"), ["selection_group"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("highlights") as batch:
        batch.drop_index(op.f("ix_highlights_selection_group"))
        batch.drop_column("analysis")
        batch.drop_column("selection_group")
    with op.batch_alter_table("clip_requests") as batch:
        batch.drop_column("analyzed_at")
