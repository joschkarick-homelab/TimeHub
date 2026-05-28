"""add transforms + target_rules on import_formats

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-28

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable so existing rows don't need a backfill; the app coerces NULL to
    # an empty list on read and always writes a concrete list.
    op.add_column("import_formats", sa.Column("transforms", sa.JSON(), nullable=True))
    op.add_column("import_formats", sa.Column("target_rules", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("import_formats", "target_rules")
    op.drop_column("import_formats", "transforms")
