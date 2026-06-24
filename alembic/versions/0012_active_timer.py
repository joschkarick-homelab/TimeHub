"""active timer (running stopwatch, one per user)

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-24

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "active_timers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One running timer per user.
        sa.UniqueConstraint("user_id", name="uq_active_timers_user"),
    )
    op.create_index("ix_active_timers_user_id", "active_timers", ["user_id"], unique=True)
    op.create_index("ix_active_timers_project_id", "active_timers", ["project_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_active_timers_project_id", table_name="active_timers")
    op.drop_index("ix_active_timers_user_id", table_name="active_timers")
    op.drop_table("active_timers")
