"""Self-service SQLite backup/restore as a single ZIP, so admins never need
host/volume access on the Hub. Backup uses VACUUM INTO for a WAL-consistent
snapshot; restore uses the sqlite3 online backup API to overwrite the live DB
with proper locking.

ZIP layout:
    db/timehub.sqlite   # the database snapshot (required)
    uploads/...         # optional uploaded files
"""

import io
import os
import sqlite3
import tempfile
import zipfile

from app.config import get_settings

_DB_ARCNAME = "db/timehub.sqlite"


def sqlite_path() -> str:
    url = get_settings().database_url
    if not url.startswith("sqlite"):
        raise RuntimeError("Backup/Restore wird nur für SQLite unterstützt")
    # sqlite:////app/data/timehub.sqlite → /app/data/timehub.sqlite
    return "/" + url.split("sqlite:///", 1)[1].lstrip("/")


def make_backup_zip(uploads_dir: str | None = None) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = os.path.join(tmp, "snapshot.sqlite")
        con = sqlite3.connect(sqlite_path())
        try:
            con.execute("VACUUM INTO ?", (snapshot,))
        finally:
            con.close()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snapshot, _DB_ARCNAME)
            if uploads_dir and os.path.isdir(uploads_dir):
                for root, _dirs, files in os.walk(uploads_dir):
                    for name in files:
                        full = os.path.join(root, name)
                        rel = os.path.relpath(full, uploads_dir)
                        zf.write(full, f"uploads/{rel}")
        return buf.getvalue()


def _validate_sqlite(path: str) -> None:
    con = sqlite3.connect(path)
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Hochgeladene Datenbank ist beschädigt")
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "users" not in tables or "time_entries" not in tables:
            raise ValueError("ZIP enthält keine gültige TimeHub-Datenbank")
    finally:
        con.close()


def restore_from_zip(data: bytes, uploads_dir: str | None = None) -> None:
    from app.db import engine

    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        if _DB_ARCNAME not in names:
            raise ValueError(f"ZIP muss {_DB_ARCNAME} enthalten")
        uploaded = os.path.join(tmp, "uploaded.sqlite")
        with open(uploaded, "wb") as fh:
            fh.write(zf.read(_DB_ARCNAME))
        _validate_sqlite(uploaded)

        # Overwrite the live DB page-by-page via the online backup API.
        engine.dispose()  # drop pooled connections first
        dest = sqlite3.connect(sqlite_path())
        source = sqlite3.connect(uploaded)
        try:
            source.backup(dest)
        finally:
            source.close()
            dest.close()
        engine.dispose()

        if uploads_dir:
            for name in names:
                if name.startswith("uploads/") and not name.endswith("/"):
                    target = os.path.join(uploads_dir, name[len("uploads/"):])
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with open(target, "wb") as fh:
                        fh.write(zf.read(name))
