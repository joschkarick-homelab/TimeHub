"""saved views for dashboard & reports

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-19

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "saved_views",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="reports"),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("date_range", sa.String(length=24), nullable=False, server_default="custom"),
        sa.Column("date_from", sa.Date(), nullable=True),
        sa.Column("date_to", sa.Date(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("customer", sa.String(length=255), nullable=True),
        sa.Column("group_by", sa.JSON(), nullable=False),
        sa.Column("detailed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "kind", "name", name="uq_saved_views_user_kind_name"),
    )
    op.create_index("ix_saved_views_user_id", "saved_views", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_saved_views_user_id", table_name="saved_views")
    op.drop_table("saved_views")
