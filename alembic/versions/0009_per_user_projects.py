"""per-user projects: add projects.user_id, code unique per (user_id, code)

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-08

Projects become per-user (owned by and visible to one user). The global unique
on `code` becomes a composite unique on `(user_id, code)`, so different users
may use the same code.

Backfill assigns ownership from existing time entries:
  * a project used by exactly one user      -> that user
  * a project used by several users          -> the heaviest user keeps it; for
    every other user a copy is created and that user's entries are repointed,
    so nobody loses visibility of their own data
  * a project with no entries                -> the first admin (or first user)
"""
from collections import defaultdict
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PROJECT_COLS = (
    "name", "code", "customer", "color", "status",
    "default_sync_target", "sync_targets", "sync_metadata",
    "created_at", "updated_at",
)


def _projects_table() -> sa.Table:
    """The projects schema as it exists right after add_column(user_id),
    used as copy_from for the SQLite batch recreate so the old single-column
    unique on `code` is dropped."""
    return sa.Table(
        "projects",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("code", sa.String(64), nullable=False, index=True),
        sa.Column("customer", sa.String(255), nullable=True),
        sa.Column("color", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("default_sync_target", sa.String(32), nullable=False),
        sa.Column("sync_targets", sa.JSON(), nullable=False),
        sa.Column("sync_metadata", sa.JSON(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.add_column("projects", sa.Column("user_id", sa.Integer(), nullable=True))
    # Phase A: assign a single primary owner to every project (updates only —
    # no duplicate codes yet, so the still-present global unique is respected).
    _assign_primary_owners()

    # Swap the global unique on `code` for a per-user one. The column-level
    # unique (and its SQLite autoindex) is removed by the batch recreate /
    # the Postgres drop_constraint.
    op.drop_index("ix_projects_code", table_name="projects")
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            "projects", copy_from=_projects_table(), recreate="always"
        ) as batch:
            batch.alter_column("user_id", existing_type=sa.Integer(), nullable=False)
            batch.create_foreign_key(
                "fk_projects_user_id", "users", ["user_id"], ["id"], ondelete="CASCADE"
            )
            batch.create_unique_constraint("uq_projects_user_code", ["user_id", "code"])
            batch.create_index("ix_projects_code", ["code"])
    else:
        op.alter_column("projects", "user_id", existing_type=sa.Integer(), nullable=False)
        op.drop_constraint("projects_code_key", "projects", type_="unique")
        op.create_foreign_key(
            "fk_projects_user_id", "projects", "users", ["user_id"], ["id"], ondelete="CASCADE"
        )
        op.create_unique_constraint("uq_projects_user_code", "projects", ["user_id", "code"])
        op.create_index("ix_projects_code", "projects", ["code"])

    # Phase B: now that codes are unique per user, split projects that were
    # shared across users — copy per extra user and repoint their entries.
    _split_shared_projects()


def _projects_core() -> sa.Table:
    return sa.table(
        "projects",
        sa.column("id", sa.Integer),
        sa.column("user_id", sa.Integer),
        *[sa.column(c, sa.JSON if c in ("sync_targets", "sync_metadata") else sa.String)
          for c in _PROJECT_COLS],
    )


def _entries_core() -> sa.Table:
    return sa.table(
        "time_entries",
        sa.column("id", sa.Integer),
        sa.column("project_id", sa.Integer),
        sa.column("user_id", sa.Integer),
    )


def _usage(conn, entries) -> dict[int, dict[int, list[int]]]:
    """project_id -> {user_id: [entry_id, ...]}"""
    usage: dict[int, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    for r in conn.execute(sa.select(entries.c.id, entries.c.project_id, entries.c.user_id)):
        usage[r.project_id][r.user_id].append(r.id)
    return usage


def _assign_primary_owners() -> None:
    conn = op.get_bind()
    projects, entries = _projects_core(), _entries_core()
    users = sa.table("users", sa.column("id", sa.Integer), sa.column("is_admin", sa.Boolean))
    usage = _usage(conn, entries)

    fallback = conn.execute(
        sa.select(users.c.id).where(users.c.is_admin.is_(True)).order_by(users.c.id).limit(1)
    ).scalar()
    if fallback is None:
        fallback = conn.execute(sa.select(users.c.id).order_by(users.c.id).limit(1)).scalar()

    for pid in [r.id for r in conn.execute(sa.select(projects.c.id))]:
        by_user = usage.get(pid, {})
        # Heaviest user (ties broken by lowest id) keeps the original project.
        primary = min(by_user.items(), key=lambda kv: (-len(kv[1]), kv[0]))[0] if by_user else fallback
        conn.execute(projects.update().where(projects.c.id == pid).values(user_id=primary))


def _split_shared_projects() -> None:
    conn = op.get_bind()
    projects, entries = _projects_core(), _entries_core()
    usage = _usage(conn, entries)

    for p in conn.execute(sa.select(projects)):
        by_user = usage.get(p.id, {})
        for uid, entry_ids in by_user.items():
            if uid == p.user_id:
                continue  # the primary owner keeps the original project
            values = {c: getattr(p, c) for c in _PROJECT_COLS}
            values["user_id"] = uid
            conn.execute(projects.insert().values(**values))
            # (user_id, code) is unique now, so this resolves the fresh copy's id.
            new_id = conn.execute(
                sa.select(projects.c.id).where(
                    projects.c.user_id == uid, projects.c.code == p.code
                )
            ).scalar()
            conn.execute(
                entries.update().where(entries.c.id.in_(entry_ids)).values(project_id=new_id)
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("projects", recreate="always") as batch:
            batch.drop_constraint("uq_projects_user_code", type_="unique")
            batch.drop_constraint("fk_projects_user_id", type_="foreignkey")
            batch.drop_column("user_id")
    else:
        op.drop_constraint("uq_projects_user_code", "projects", type_="unique")
        op.drop_constraint("fk_projects_user_id", "projects", type_="foreignkey")
        op.drop_column("projects", "user_id")
    # Restore a plain index on code. NOTE: the original constraint was a GLOBAL
    # unique; it is intentionally not recreated as unique here because the
    # upgrade may have split shared projects into per-user duplicate codes,
    # which a global unique could no longer accommodate.
    op.drop_index("ix_projects_code", table_name="projects")
    op.create_index("ix_projects_code", "projects", ["code"])
