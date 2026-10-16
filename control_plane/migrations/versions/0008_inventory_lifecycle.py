"""Guard inventory removals against incomplete source snapshots."""

import sqlalchemy as sa

from alembic import op

revision = "0008_inventory_lifecycle"
down_revision = "0007_plan_dependency_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    collector_columns = {column["name"] for column in sa.inspect(bind).get_columns("collectors")}
    inventory_columns = {column["name"] for column in sa.inspect(bind).get_columns("inventory_vms")}
    if "last_successful_inventory_at" not in collector_columns:
        op.add_column(
            "collectors",
            sa.Column("last_successful_inventory_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "lifecycle_status" not in inventory_columns:
        op.add_column(
            "inventory_vms",
            sa.Column(
                "lifecycle_status", sa.String(length=32), nullable=False, server_default="active"
            ),
        )
    if "pending_removal_count" not in inventory_columns:
        op.add_column(
            "inventory_vms",
            sa.Column("pending_removal_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if "removal_requires_confirmation" not in inventory_columns:
        op.add_column(
            "inventory_vms",
            sa.Column(
                "removal_requires_confirmation",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    op.execute(
        "UPDATE inventory_vms SET lifecycle_status = "
        "CASE WHEN is_active THEN 'active' ELSE 'inactive' END"
    )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("inventory_vms")}
    if "ix_inventory_vms_lifecycle_status" not in indexes:
        op.create_index("ix_inventory_vms_lifecycle_status", "inventory_vms", ["lifecycle_status"])


def downgrade() -> None:
    raise NotImplementedError("Inventory lifecycle evidence is retained for migration safety.")
