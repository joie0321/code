"""Store planner decisions for normalized dependency-review relationships."""

from alembic import op
import sqlalchemy as sa


revision = "0004_dependency_decisions"
down_revision = "0003_vm_state_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dependency_decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("collector_id", sa.String(length=36), nullable=False),
        sa.Column("source_vm_uuid", sa.String(length=64), nullable=False),
        sa.Column("destination_identity", sa.String(length=64), nullable=False),
        sa.Column("protocol", sa.String(length=8), nullable=False),
        sa.Column("port_key", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["collector_id"], ["collectors.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "collector_id",
            "source_vm_uuid",
            "destination_identity",
            "protocol",
            "port_key",
            "category",
            name="uq_dependency_decision",
        ),
    )
    op.create_index("ix_dependency_decisions_collector_id", "dependency_decisions", ["collector_id"])
    op.create_index("ix_dependency_decisions_source_vm_uuid", "dependency_decisions", ["source_vm_uuid"])
    op.create_index("ix_dependency_decisions_destination_identity", "dependency_decisions", ["destination_identity"])
    op.create_index("ix_dependency_decisions_updated_at", "dependency_decisions", ["updated_at"])


def downgrade() -> None:
    op.drop_table("dependency_decisions")
