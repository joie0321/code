"""Add indexes used by scoped Current Environment observation queries."""

from alembic import op

revision = "0011_current_env_perf"
down_revision = "0010_traffic_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_observation_batches_collector_received_at",
        "observation_batches",
        ["collector_id", "received_at"],
        unique=False,
    )
    op.create_index(
        "ix_observations_collector_batch_observed_at",
        "observations",
        ["collector_id", "observation_batch_id", "observed_at"],
        unique=False,
    )


def downgrade() -> None:
    raise NotImplementedError("Performance indexes are retained for migration safety.")
