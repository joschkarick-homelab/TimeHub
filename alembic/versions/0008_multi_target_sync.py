"""multi-target sync: entry_syncs + sync_rules tables, project/entry target sets

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-08

Phase 0 of the bundled multi-target export concept (docs/export-konzept.md):

  * `entry_syncs`  — one row per (entry × target) holding per-target status,
    remote reference, attempts and last error.
  * `sync_rules`   — declarative rules that refine an entry's target set.
  * `projects.sync_targets` (JSON list) — default target set per project.
  * `time_entries.sync_targets_override` (JSON list) — per-entry override.

Backfill keeps the existing single-target world intact: each project's
`default_sync_target` seeds its `sync_targets` list, and every entry gets one
`entry_syncs` row for its effective target carrying its current `sync_status`.
intern/none never get a row.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NON_SYNC = ("intern", "none")


def upgrade() -> None:
    op.create_table(
        "entry_syncs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "entry_id",
            sa.Integer(),
            sa.ForeignKey("time_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("external_ref", sa.String(length=255), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("entry_id", "target", name="uq_entry_syncs_entry_target"),
    )
    op.create_index("ix_entry_syncs_entry_id", "entry_syncs", ["entry_id"])
    op.create_index("ix_entry_syncs_external_ref", "entry_syncs", ["external_ref"])

    op.create_table(
        "sync_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="global"),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("condition", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("target", sa.String(length=32), nullable=True),
        sa.Column("targets", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_sync_rules_priority", "sync_rules", ["priority"])
    op.create_index("ix_sync_rules_project_id", "sync_rules", ["project_id"])

    with op.batch_alter_table("projects") as batch:
        batch.add_column(
            sa.Column("sync_targets", sa.JSON(), nullable=False, server_default="[]")
        )
    with op.batch_alter_table("time_entries") as batch:
        batch.add_column(sa.Column("sync_targets_override", sa.JSON(), nullable=True))

    _backfill()


def _backfill() -> None:
    conn = op.get_bind()
    projects = sa.table(
        "projects",
        sa.column("id", sa.Integer),
        sa.column("default_sync_target", sa.String),
        sa.column("sync_targets", sa.JSON),
    )
    entries = sa.table(
        "time_entries",
        sa.column("id", sa.Integer),
        sa.column("project_id", sa.Integer),
        sa.column("sync_target_override", sa.String),
        sa.column("sync_status", sa.String),
    )
    entry_syncs = sa.table(
        "entry_syncs",
        sa.column("entry_id", sa.Integer),
        sa.column("target", sa.String),
        sa.column("status", sa.String),
    )

    # Seed project default target sets from the single default target.
    proj_default: dict[int, str] = {}
    for row in conn.execute(sa.select(projects.c.id, projects.c.default_sync_target)):
        proj_default[row.id] = row.default_sync_target
        targets = [] if row.default_sync_target in _NON_SYNC else [row.default_sync_target]
        conn.execute(
            projects.update().where(projects.c.id == row.id).values(sync_targets=targets)
        )

    # One entry_syncs row per entry for its effective target.
    rows = conn.execute(
        sa.select(
            entries.c.id,
            entries.c.project_id,
            entries.c.sync_target_override,
            entries.c.sync_status,
        )
    )
    for r in rows:
        target = r.sync_target_override or proj_default.get(r.project_id)
        if not target or target in _NON_SYNC:
            continue
        conn.execute(
            entry_syncs.insert().values(
                entry_id=r.id, target=target, status=r.sync_status or "pending"
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("time_entries") as batch:
        batch.drop_column("sync_targets_override")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("sync_targets")
    op.drop_index("ix_sync_rules_project_id", table_name="sync_rules")
    op.drop_index("ix_sync_rules_priority", table_name="sync_rules")
    op.drop_table("sync_rules")
    op.drop_index("ix_entry_syncs_external_ref", table_name="entry_syncs")
    op.drop_index("ix_entry_syncs_entry_id", table_name="entry_syncs")
    op.drop_table("entry_syncs")
