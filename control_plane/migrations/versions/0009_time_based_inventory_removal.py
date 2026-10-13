"""Use elapsed absence for inventory removal."""

import sqlalchemy as sa

from alembic import op

revision = "0009_time_removal"
down_revision = "0008_inventory_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("inventory_vms")}
    if "missing_since" not in columns:
        op.add_column(
            "inventory_vms", sa.Column("missing_since", sa.DateTime(timezone=True), nullable=True)
        )
    op.execute(
        "UPDATE inventory_vms SET lifecycle_status = 'missing', missing_since = updated_at "
        "WHERE lifecycle_status = 'pending_removal'"
    )


def downgrade() -> None:
    raise NotImplementedError("Inventory lifecycle evidence is retained for migration safety.")
