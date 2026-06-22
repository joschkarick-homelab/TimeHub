"""add indexes on the sync-status filter columns

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-16

"""
from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_time_entries_sync_status", "time_entries", ["sync_status"], unique=False
    )
    op.create_index(
        "ix_entry_syncs_status", "entry_syncs", ["status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_entry_syncs_status", table_name="entry_syncs")
    op.drop_index("ix_time_entries_sync_status", table_name="time_entries")
