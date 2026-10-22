"""rolled-back runs: run_date no longer unique

Revision ID: 53d6216b3924
Revises: d91e82e01acc
Create Date: 2026-10-22 15:42:49.521490

"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401
from alembic import op

NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


# revision identifiers, used by Alembic.
revision: str = "53d6216b3924"
down_revision: str | Sequence[str] | None = "d91e82e01acc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """run_date stops being unique: a rolled-back run stays for audit next to its replacement.

    The initial migration created an unnamed unique constraint. Postgres names it
    planning_run_run_date_key; SQLite needs a naming convention so batch mode can find it.
    """
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("planning_run", recreate="always", naming_convention=NAMING) as batch_op:
            batch_op.drop_constraint("uq_planning_run_run_date", type_="unique")
            batch_op.create_index("ix_planning_run_run_date", ["run_date"], unique=False)
    else:
        op.drop_constraint("planning_run_run_date_key", "planning_run", type_="unique")
        op.create_index("ix_planning_run_run_date", "planning_run", ["run_date"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_planning_run_run_date", table_name="planning_run")
    with op.batch_alter_table("planning_run", naming_convention=NAMING) as batch_op:
        batch_op.create_unique_constraint("uq_planning_run_run_date", ["run_date"])
