import datetime
import os
import tempfile

import pytest
from sqlalchemy import create_engine, select

from app import models  # noqa: F401 — register metadata
from app.db import Base
from app.models import Project, TimeEntry, User
from scripts.migrate_pg_to_sqlite import copy_all


def _seed_source():
    """Build an in-memory SQLite source with a user, a project (FK → user) and a
    time entry (FK → user + project, with a non-empty JSON ``tags``)."""
    src = create_engine("sqlite://")  # in-memory source
    Base.metadata.create_all(src)
    with src.begin() as conn:
        conn.execute(
            User.__table__.insert(),
            [
                {"id": 1, "email": "a@x.de", "full_name": "A", "is_admin": True,
                 "is_active": True},
                {"id": 2, "email": "b@x.de", "full_name": "B", "is_admin": False,
                 "is_active": True},
            ],
        )
        conn.execute(
            Project.__table__.insert(),
            [{"id": 10, "user_id": 1, "name": "Proj", "code": "P1"}],
        )
        conn.execute(
            TimeEntry.__table__.insert(),
            [
                {
                    "id": 100,
                    "user_id": 1,
                    "project_id": 10,
                    "entry_date": datetime.date(2026, 6, 26),
                    "duration_minutes": 90,
                    "tags": ["a", "b"],
                }
            ],
        )
    return src


def _fresh_target():
    fd, dst_path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    return create_engine(f"sqlite:///{dst_path}")


def test_copy_all_transfers_rows_in_fk_order():
    # Source and target are both SQLite here (the copy logic is engine-agnostic).
    src = _seed_source()
    dst = _fresh_target()

    copy_all(src, dst)

    with dst.connect() as conn:
        # Parent users copied.
        assert conn.execute(select(User.email).order_by(User.id)).scalars().all() == [
            "a@x.de",
            "b@x.de",
        ]
        # Child project copied, FK points at the right parent.
        proj = conn.execute(
            select(Project.id, Project.user_id, Project.code)
        ).one()
        assert proj == (10, 1, "P1")
        # Child time entry copied, FKs match parents, JSON tags round-tripped.
        entry = conn.execute(
            select(TimeEntry.id, TimeEntry.user_id, TimeEntry.project_id, TimeEntry.tags)
        ).one()
        assert entry.id == 100
        assert entry.user_id == 1
        assert entry.project_id == 10
        assert entry.tags == ["a", "b"]


def test_copy_all_refuses_non_empty_target():
    src = _seed_source()
    dst = _fresh_target()

    copy_all(src, dst)  # first run populates the target

    with pytest.raises(RuntimeError, match="already contains data"):
        copy_all(src, dst)  # second run must fail fast, not corrupt the DB
