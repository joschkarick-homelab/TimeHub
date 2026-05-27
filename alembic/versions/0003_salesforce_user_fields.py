"""add salesforce_user_id + salesforce_contact_id on users

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-27

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("salesforce_user_id", sa.String(32), nullable=True))
    op.add_column("users", sa.Column("salesforce_contact_id", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "salesforce_contact_id")
    op.drop_column("users", "salesforce_user_id")
