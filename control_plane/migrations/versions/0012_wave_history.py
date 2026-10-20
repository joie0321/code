"""Retain the last automatic recommendation before inventory changes."""

import sqlalchemy as sa
from alembic import op

revision = "0012_wave_history"
down_revision = "0011_current_env_perf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table_name = "recommended_wave_snapshots"
    index_name = "ix_recommended_wave_snapshots_collector_captured"
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        op.create_table(
            table_name,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("collector_id", sa.String(length=36), nullable=False),
            sa.Column("inventory_batch_id", sa.String(length=36), nullable=False),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("waves", sa.JSON(), nullable=False),
            sa.ForeignKeyConstraint(["collector_id"], ["collectors.id"]),
            sa.ForeignKeyConstraint(["inventory_batch_id"], ["inventory_batches.id"]),
            sa.UniqueConstraint(
                "collector_id", "inventory_batch_id", name="uq_wave_snapshot_inventory_batch"
            ),
        )

    existing_indexes = {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }
    if index_name not in existing_indexes:
        op.create_index(index_name, table_name, ["collector_id", "captured_at"], unique=False)


def downgrade() -> None:
    raise NotImplementedError("Recommendation history is retained for reporting continuity.")
