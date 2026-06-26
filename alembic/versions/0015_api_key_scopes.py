"""api key scopes and expiry

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-26

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing keys default to full access with no expiry — behaviour unchanged.
    op.add_column(
        "api_keys",
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="read_write"),
    )
    op.add_column(
        "api_keys",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "expires_at")
    op.drop_column("api_keys", "scope")
