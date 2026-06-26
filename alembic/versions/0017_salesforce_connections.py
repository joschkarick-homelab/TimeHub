"""salesforce per-user oauth connections

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-26

Additive: a new ``salesforce_connections`` table holding per-user Salesforce
OAuth tokens (one row per user). Nothing on existing tables changes — the global
service-account credentials in ``app_settings`` keep working as the fallback.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "salesforce_connections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("instance_url", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("access_token", sa.Text(), nullable=False, server_default=""),
        sa.Column("refresh_token", sa.Text(), nullable=False, server_default=""),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "connected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One Salesforce connection per user.
        sa.UniqueConstraint("user_id", name="uq_salesforce_connections_user"),
    )
    op.create_index(
        "ix_salesforce_connections_user_id", "salesforce_connections", ["user_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index(
        "ix_salesforce_connections_user_id", table_name="salesforce_connections"
    )
    op.drop_table("salesforce_connections")
