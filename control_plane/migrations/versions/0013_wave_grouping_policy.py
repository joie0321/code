"""Store per-collector approved dynamic application ports."""

import sqlalchemy as sa
from alembic import op

revision = "0013_wave_grouping_policy"
down_revision = "0012_wave_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add a deny-by-default override list without altering retained traffic."""

    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("collectors")}
    if "wave_grouping_port_allowlist" not in columns:
        op.add_column(
            "collectors",
            sa.Column(
                "wave_grouping_port_allowlist",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
        )


def downgrade() -> None:
    raise NotImplementedError("Wave-grouping policy is retained for migration safety.")
