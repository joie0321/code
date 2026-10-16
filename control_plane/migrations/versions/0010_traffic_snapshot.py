"""Track the traffic upload that defines the Latest dashboard snapshot."""

import sqlalchemy as sa

from alembic import op

revision = "0010_traffic_snapshot"
down_revision = "0009_time_removal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    collector_columns = {column["name"] for column in sa.inspect(bind).get_columns("collectors")}
    observation_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("observations")
    }
    if "last_successful_traffic_at" not in collector_columns:
        op.add_column(
            "collectors",
            sa.Column("last_successful_traffic_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "last_successful_traffic_batch_id" not in collector_columns:
        op.add_column(
            "collectors",
            sa.Column("last_successful_traffic_batch_id", sa.String(length=36), nullable=True),
        )
    if "observation_batch_id" not in observation_columns:
        op.add_column(
            "observations",
            sa.Column("observation_batch_id", sa.String(length=36), nullable=True),
        )
        if bind.dialect.name != "sqlite":
            op.create_foreign_key(
                "fk_observations_observation_batch",
                "observations",
                "observation_batches",
                ["observation_batch_id"],
                ["id"],
            )
        op.create_index(
            "ix_observations_observation_batch_id", "observations", ["observation_batch_id"]
        )

    # Legacy observations were accepted before an observation-to-batch link
    # existed.  Associate retained evidence with its collector's latest known
    # upload so the first Latest view after upgrade remains informative.
    op.execute(
        "UPDATE observations SET observation_batch_id = ("
        "SELECT id FROM observation_batches "
        "WHERE observation_batches.collector_id = observations.collector_id "
        "ORDER BY received_at DESC LIMIT 1"
        ") WHERE observation_batch_id IS NULL"
    )
    op.execute(
        "UPDATE collectors SET last_successful_traffic_batch_id = ("
        "SELECT id FROM observation_batches "
        "WHERE observation_batches.collector_id = collectors.id "
        "ORDER BY received_at DESC LIMIT 1"
        ") WHERE last_successful_traffic_batch_id IS NULL"
    )
    op.execute(
        "UPDATE collectors SET last_successful_traffic_at = ("
        "SELECT received_at FROM observation_batches "
        "WHERE observation_batches.id = collectors.last_successful_traffic_batch_id"
        ") WHERE last_successful_traffic_at IS NULL"
    )


def downgrade() -> None:
    raise NotImplementedError("Traffic upload evidence is retained for migration safety.")
