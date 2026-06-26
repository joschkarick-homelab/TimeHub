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


def _login_non_admin(client, email="backup-other@example.com"):
    """Create a non-admin user (via the admin API) and start a web session for it."""
    admin_token = client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "testpass"}
    ).json()["access_token"]
    client.post(
        "/api/v1/users",
        json={"email": email, "password": "secret123", "full_name": "Other U", "is_admin": False},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    r = client.post("/login", data={"email": email, "password": "secret123"},
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


def test_restore_rejects_path_traversal_in_uploads(tmp_path):
    # A ZIP with a valid DB (so validation passes) PLUS a traversal upload entry
    # must be rejected, and must NOT write anything outside uploads_dir.
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    db_bytes = make_backup_zip(uploads_dir=None)  # valid db/timehub.sqlite ZIP
    with zipfile.ZipFile(io.BytesIO(db_bytes)) as zf:
        db_payload = zf.read("db/timehub.sqlite")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("db/timehub.sqlite", db_payload)
        zf.writestr("uploads/../../evil.txt", b"pwned")

    with pytest.raises(ValueError):
        restore_from_zip(buf.getvalue(), uploads_dir=str(uploads))

    # Nothing escaped: neither the uploads' parent nor grandparent gained evil.txt.
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path.parent / "evil.txt").exists()


def test_backup_endpoint_requires_admin_and_streams_zip(client):
    _login(client)
    r = client.get("/admin/backup")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert "db/timehub.sqlite" in zf.namelist()


def test_backup_endpoint_rejects_anonymous(client):
    r = client.get("/admin/backup", follow_redirects=False)
    # No session → redirect to login (or 403), never a 200 ZIP stream.
    assert r.status_code != 200
    assert r.status_code in (302, 401, 403)


def test_restore_endpoint_rejects_anonymous(client):
    # CSRF header is set by the client fixture, so this gets past CSRF and is
    # rejected purely on auth grounds.
    r = client.post(
        "/admin/restore",
        files={"file": ("x.zip", b"not-a-zip", "application/zip")},
        follow_redirects=False,
    )
    assert r.status_code != 200
    assert r.status_code in (302, 401, 403)


def test_backup_endpoint_rejects_non_admin(client):
    _login_non_admin(client)
    r = client.get("/admin/backup", follow_redirects=False)
    assert r.status_code == 403


def test_restore_endpoint_rejects_non_admin(client):
    _login_non_admin(client, email="backup-other2@example.com")
    r = client.post(
        "/admin/restore",
        files={"file": ("x.zip", b"not-a-zip", "application/zip")},
        follow_redirects=False,
    )
    assert r.status_code == 403
