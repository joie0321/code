"""Record append-only VM inventory state evidence.

Revision ID: 0003_vm_state_history
Revises: 0002_expand_collector_type
Create Date: 2026-09-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_vm_state_history"
down_revision = "0002_expand_collector_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inventory_vm_states",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("collector_id", sa.String(length=36), nullable=False),
        sa.Column("vm_uuid", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("ips", sa.Text(), nullable=False, server_default=""),
        sa.Column("power_state", sa.String(length=32), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["collector_id"], ["collectors.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_inventory_vm_states_collector_id", "inventory_vm_states", ["collector_id"]
    )
    op.create_index("ix_inventory_vm_states_vm_uuid", "inventory_vm_states", ["vm_uuid"])
    op.create_index("ix_inventory_vm_states_is_active", "inventory_vm_states", ["is_active"])
    op.create_index(
        "ix_inventory_vm_states_captured_at", "inventory_vm_states", ["captured_at"]
    )


def downgrade() -> None:
    raise NotImplementedError("Inventory state history is retained for migration evidence.")
