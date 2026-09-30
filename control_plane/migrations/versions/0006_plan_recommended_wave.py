"""Retain each VM's original automatic wave recommendation."""

from alembic import op
import sqlalchemy as sa


revision = "0006_plan_recommended_wave"
down_revision = "0005_migration_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "migration_plan_vms",
        sa.Column("recommended_wave_number", sa.Integer(), nullable=True),
    )
    # Plans created before this release did not retain a separate baseline.
    # Preserve their then-current assignment as the selectable recommendation.
    op.execute(
        "UPDATE migration_plan_vms "
        "SET recommended_wave_number = wave_number "
        "WHERE recommended_wave_number IS NULL AND wave_number IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("migration_plan_vms", "recommended_wave_number")
