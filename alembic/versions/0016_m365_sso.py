"""m365 SSO: optional password + entra object id on users

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-26

Additive and data-preserving: existing users keep their password hash and all
data. ``hashed_password`` becomes nullable so SSO-only accounts (no local
password) are representable, and a nullable, unique ``entra_oid`` column links a
TimeHub user to their Entra object id on first single sign-on.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # One batch op so SQLite (table-rebuild) and Postgres (in-place ALTERs) both
    # apply the column add, nullability relax and index in a single pass.
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("entra_oid", sa.String(length=64), nullable=True))
        batch.alter_column(
            "hashed_password", existing_type=sa.String(length=255), nullable=True
        )
        batch.create_index("ix_users_entra_oid", ["entra_oid"], unique=True)


def downgrade() -> None:
    # Restore NOT NULL: any SSO-only rows (no password) get an empty hash first
    # so the constraint can be re-applied without failing.
    op.execute("UPDATE users SET hashed_password = '' WHERE hashed_password IS NULL")
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_entra_oid")
        batch.alter_column(
            "hashed_password", existing_type=sa.String(length=255), nullable=False
        )
        batch.drop_column("entra_oid")
