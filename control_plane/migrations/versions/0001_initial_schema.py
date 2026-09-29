"""Create the initial control-plane schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "enrollments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("code_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_enrollments_tenant_id", "enrollments", ["tenant_id"], unique=False)
    op.create_index("ix_enrollments_code_digest", "enrollments", ["code_digest"], unique=True)
    op.create_index("ix_enrollments_expires_at", "enrollments", ["expires_at"], unique=False)

    op.create_table(
        "collectors",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("agent_token_digest", sa.String(length=64), nullable=False),
        sa.Column("software_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="online"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_collectors_tenant_id", "collectors", ["tenant_id"], unique=False)
    op.create_index(
        "ix_collectors_agent_token_digest", "collectors", ["agent_token_digest"], unique=True
    )
    op.create_index("ix_collectors_status", "collectors", ["status"], unique=False)
    op.create_index("ix_collectors_last_seen_at", "collectors", ["last_seen_at"], unique=False)

    op.create_table(
        "reconnection_codes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("collector_id", sa.String(length=36), nullable=False),
        sa.Column("code_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["collector_id"], ["collectors.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reconnection_codes_collector_id", "reconnection_codes", ["collector_id"])
    op.create_index(
        "ix_reconnection_codes_code_digest", "reconnection_codes", ["code_digest"], unique=True
    )
    op.create_index("ix_reconnection_codes_expires_at", "reconnection_codes", ["expires_at"])

    op.create_table(
        "observation_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("collector_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["collector_id"], ["collectors.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("collector_id", "sequence", name="uq_batch_sequence"),
    )
    op.create_index("ix_observation_batches_collector_id", "observation_batches", ["collector_id"])
    op.create_index("ix_observation_batches_received_at", "observation_batches", ["received_at"])

    op.create_table(
        "observations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("collector_id", sa.String(length=36), nullable=False),
        sa.Column("source_vm_uuid", sa.String(length=64), nullable=False),
        sa.Column("source_ip", sa.String(length=45), nullable=False),
        sa.Column("destination_ip", sa.String(length=45), nullable=False),
        sa.Column("destination_port", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.String(length=8), nullable=False),
        sa.Column("collector_type", sa.String(length=16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("process", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["collector_id"], ["collectors.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_observations_collector_id", "observations", ["collector_id"])
    op.create_index("ix_observations_source_vm_uuid", "observations", ["source_vm_uuid"])
    op.create_index("ix_observations_source_ip", "observations", ["source_ip"])
    op.create_index("ix_observations_destination_ip", "observations", ["destination_ip"])
    op.create_index("ix_observations_observed_at", "observations", ["observed_at"])

    op.create_table(
        "inventory_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("collector_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["collector_id"], ["collectors.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("collector_id", "sequence", name="uq_inventory_batch_sequence"),
    )
    op.create_index("ix_inventory_batches_collector_id", "inventory_batches", ["collector_id"])
    op.create_index("ix_inventory_batches_received_at", "inventory_batches", ["received_at"])

    op.create_table(
        "inventory_vms",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("collector_id", sa.String(length=36), nullable=False),
        sa.Column("vm_uuid", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=True),
        sa.Column("ips", sa.Text(), nullable=False, server_default=""),
        sa.Column("cluster", sa.String(length=255), nullable=True),
        sa.Column("folder", sa.String(length=1024), nullable=True),
        sa.Column("os_name", sa.String(length=512), nullable=True),
        sa.Column("power_state", sa.String(length=32), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["collector_id"], ["collectors.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("collector_id", "vm_uuid", name="uq_collector_vm"),
    )
    op.create_index("ix_inventory_vms_collector_id", "inventory_vms", ["collector_id"])
    op.create_index("ix_inventory_vms_vm_uuid", "inventory_vms", ["vm_uuid"])
    op.create_index("ix_inventory_vms_is_active", "inventory_vms", ["is_active"])
    op.create_index("ix_inventory_vms_updated_at", "inventory_vms", ["updated_at"])


def downgrade() -> None:
    raise NotImplementedError("The initial schema migration is intentionally irreversible.")
