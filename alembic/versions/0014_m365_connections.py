"""m365 calendar connections (per-user OAuth tokens)

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-26

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "m365_connections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("access_token", sa.Text(), nullable=False, server_default=""),
        sa.Column("refresh_token", sa.Text(), nullable=False, server_default=""),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "connected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One connected mailbox per user.
        sa.UniqueConstraint("user_id", name="uq_m365_connections_user"),
    )
    op.create_index(
        "ix_m365_connections_user_id", "m365_connections", ["user_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_m365_connections_user_id", table_name="m365_connections")
    op.drop_table("m365_connections")
