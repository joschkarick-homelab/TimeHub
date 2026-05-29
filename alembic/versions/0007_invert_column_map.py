"""invert import_formats.column_map from {source: target} to {target: source}

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-29

The mapping editor became target-oriented; the canonical column_map direction
flipped accordingly. This is a one-time inversion of existing rows.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_t = sa.table("import_formats", sa.column("id", sa.Integer), sa.column("column_map", sa.JSON))


def _flip(cm: dict) -> dict:
    return {str(v): str(k) for k, v in cm.items()}


def upgrade() -> None:
    conn = op.get_bind()
    for row in conn.execute(sa.select(_t.c.id, _t.c.column_map)):
        cm = row.column_map
        if isinstance(cm, dict) and cm:
            conn.execute(_t.update().where(_t.c.id == row.id).values(column_map=_flip(cm)))


def downgrade() -> None:
    conn = op.get_bind()
    for row in conn.execute(sa.select(_t.c.id, _t.c.column_map)):
        cm = row.column_map
        if isinstance(cm, dict) and cm:
            conn.execute(_t.update().where(_t.c.id == row.id).values(column_map=_flip(cm)))
