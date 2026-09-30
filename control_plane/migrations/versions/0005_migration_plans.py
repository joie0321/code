"""Add versioned planner-owned migration plans."""

from alembic import op
import sqlalchemy as sa


revision = "0005_migration_plans"
down_revision = "0004_dependency_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "migration_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("collector_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("planner_name", sa.String(length=128), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["collector_id"], ["collectors.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("collector_id", "version", name="uq_migration_plan_version"),
    )
    op.create_index("ix_migration_plans_collector_id", "migration_plans", ["collector_id"])
    op.create_index("ix_migration_plans_status", "migration_plans", ["status"])
    op.create_index("ix_migration_plans_created_at", "migration_plans", ["created_at"])
    op.create_index("ix_migration_plans_updated_at", "migration_plans", ["updated_at"])
    op.create_table(
        "migration_plan_vms",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("vm_uuid", sa.String(length=64), nullable=False),
        sa.Column("vm_name", sa.String(length=255), nullable=False),
        sa.Column("wave_number", sa.Integer(), nullable=True),
        sa.Column("disposition", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["plan_id"], ["migration_plans.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "vm_uuid", name="uq_migration_plan_vm"),
    )
    op.create_index("ix_migration_plan_vms_plan_id", "migration_plan_vms", ["plan_id"])
    op.create_index("ix_migration_plan_vms_vm_uuid", "migration_plan_vms", ["vm_uuid"])
    op.create_index("ix_migration_plan_vms_wave_number", "migration_plan_vms", ["wave_number"])
    op.create_index("ix_migration_plan_vms_disposition", "migration_plan_vms", ["disposition"])


def downgrade() -> None:
    op.drop_table("migration_plan_vms")
    op.drop_table("migration_plans")
