import os
import tempfile

from sqlalchemy import create_engine, select

from app import models  # noqa: F401 — register metadata
from app.db import Base
from app.models import User
from scripts.migrate_pg_to_sqlite import copy_all


def test_copy_all_transfers_rows_in_fk_order():
    # Source and target are both SQLite here (the copy logic is engine-agnostic).
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

    fd, dst_path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    dst = create_engine(f"sqlite:///{dst_path}")

    copy_all(src, dst)

    with dst.connect() as conn:
        rows = conn.execute(select(User.email).order_by(User.id)).scalars().all()
    assert rows == ["a@x.de", "b@x.de"]
