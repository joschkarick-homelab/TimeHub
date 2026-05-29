"""add sample_data on import_formats

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-29

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("import_formats", sa.Column("sample_data", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("import_formats", "sample_data")
