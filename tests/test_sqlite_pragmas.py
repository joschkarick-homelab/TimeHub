from sqlalchemy import text

from app.db import engine


def test_sqlite_runs_in_wal_mode_with_busy_timeout():
    if not engine.url.drivername.startswith("sqlite"):
        import pytest

        pytest.skip("pragma test only relevant for SQLite")
    with engine.connect() as conn:
        journal = conn.execute(text("PRAGMA journal_mode")).scalar()
        busy = conn.execute(text("PRAGMA busy_timeout")).scalar()
        fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
    assert str(journal).lower() == "wal"
    assert int(busy) >= 5000
    assert int(fk) == 1
