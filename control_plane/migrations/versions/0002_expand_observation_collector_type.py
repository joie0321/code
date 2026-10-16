"""Expand observation collector type for AWS flow-log telemetry.

Revision ID: 0002_expand_collector_type
Revises: 0001_initial_schema
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_expand_collector_type"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("observations") as batch:
            batch.alter_column(
                "collector_type",
                existing_type=sa.String(length=16),
                type_=sa.String(length=32),
                existing_nullable=False,
            )
        return
    op.alter_column(
        "observations",
        "collector_type",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrading collector_type could truncate existing telemetry and is not supported."
    )
