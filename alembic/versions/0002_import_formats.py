"""import_formats

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-27

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "import_formats",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("source_hint", sa.String(64), nullable=False, server_default="custom"),
        sa.Column("separator", sa.String(4), nullable=False, server_default=","),
        sa.Column("encoding", sa.String(16), nullable=False, server_default="utf-8"),
        sa.Column("date_format", sa.String(32), nullable=False, server_default="%Y-%m-%d"),
        sa.Column("time_format", sa.String(32), nullable=False, server_default="%H:%M"),
        sa.Column("column_map", sa.JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("default_project_code", sa.String(64), nullable=True),
        sa.Column(
            "owner_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("is_global", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("notes", sa.String(1024), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_import_formats_owner_id", "import_formats", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_import_formats_owner_id", table_name="import_formats")
    op.drop_table("import_formats")
