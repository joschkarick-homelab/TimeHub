"""add msq_user_id

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-26

Additive: a new nullable, unique-indexed ``users.msq_user_id`` column holding the
stable Hub subject id from ``X-MSQ-User-Id``. Primary match key behind the Agent
Hub; nullable because existing (migrated) users have no Hub subject yet.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("msq_user_id", sa.String(length=128), nullable=True))
    op.create_index("ix_users_msq_user_id", "users", ["msq_user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_msq_user_id", table_name="users")
    op.drop_column("users", "msq_user_id")
