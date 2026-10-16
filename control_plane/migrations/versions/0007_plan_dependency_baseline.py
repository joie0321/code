"""Persist normalized dependency evidence for approved migration plans."""

from alembic import op
import sqlalchemy as sa


revision = "0007_plan_dependency_baseline"
down_revision = "0006_plan_recommended_wave"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "migration_plan_dependencies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("source_vm_uuid", sa.String(length=64), nullable=False),
        sa.Column("destination_identity", sa.String(length=64), nullable=False),
        sa.Column("protocol", sa.String(length=8), nullable=False),
        sa.Column("port_key", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["migration_plans.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_id",
            "source_vm_uuid",
            "destination_identity",
            "protocol",
            "port_key",
            "category",
            name="uq_migration_plan_dependency",
        ),
    )
    op.create_index(
        "ix_migration_plan_dependencies_plan_id",
        "migration_plan_dependencies",
        ["plan_id"],
    )
    op.create_index(
        "ix_migration_plan_dependencies_source_vm_uuid",
        "migration_plan_dependencies",
        ["source_vm_uuid"],
    )
    op.create_index(
        "ix_migration_plan_dependencies_destination_identity",
        "migration_plan_dependencies",
        ["destination_identity"],
    )


def downgrade() -> None:
    op.drop_table("migration_plan_dependencies")
