"""active timer project becomes optional

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-24

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A timer may now be started without a project and have one assigned later.
    with op.batch_alter_table("active_timers") as batch:
        batch.alter_column("project_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("active_timers") as batch:
        batch.alter_column("project_id", existing_type=sa.Integer(), nullable=False)
