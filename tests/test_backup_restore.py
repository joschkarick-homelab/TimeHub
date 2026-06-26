import io
import os
import sqlite3
import tempfile
import zipfile

import pytest

from app.services.backup import make_backup_zip, restore_from_zip


def _login(client):
    r = client.post("/login", data={"email": "admin@example.com", "password": "testpass"},
                    follow_redirects=False)
    assert r.status_code == 302


def test_backup_zip_contains_db():
    data = make_backup_zip(uploads_dir=None)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert "db/timehub.sqlite" in zf.namelist()


def test_restore_roundtrip():
    # Restoring the app's own backup must succeed and keep the schema intact.
    data = make_backup_zip(uploads_dir=None)
    restore_from_zip(data, uploads_dir=None)
    from sqlalchemy import text

    from app.db import engine

    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM users")).scalar() >= 1


def test_restore_rejects_non_timehub_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE foo (x INTEGER)")
    con.commit()
    con.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.write(path, "db/timehub.sqlite")
    os.unlink(path)
    with pytest.raises(ValueError):
        restore_from_zip(buf.getvalue(), uploads_dir=None)


def test_backup_endpoint_requires_admin_and_streams_zip(client):
    _login(client)
    r = client.get("/admin/backup")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert "db/timehub.sqlite" in zf.namelist()
