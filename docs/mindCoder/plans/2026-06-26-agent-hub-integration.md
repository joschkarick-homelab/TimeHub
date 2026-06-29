# TimeHub → mindsquare Agent Hub Migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use mindCoder:subagent-driven-development (recommended) or mindCoder:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** TimeHub als **Managed**-App hinter den mindsquare Agent Hub (`aiforge.msr2.de`) bringen — Identität aus `X-MSQ-*`-Headern statt eigenem Login, SQLite-im-Volume statt eigenem Postgres-Container, Image+Repo auf `mindcode.mindsquare.de`.

**Architecture:** Der Hub macht TLS, Domain, Entra-SSO, Session und liefert pro Request `X-MSQ-*`-Header. TimeHub bleibt ein einzelnes FastAPI-Image (uvicorn, `EXPOSE 8000`), liest die Identität aus einer neuen Identity-Middleware, provisioniert/matcht den TimeHub-User und persistiert in eine SQLite-Datei in einem benannten Volume (`appdata-timehub-data`). Eigenes Passwort-Login, M365-SSO-Login und der separate Postgres-Container entfallen. Salesforce (Service-User + per-User-OAuth + Background-Sync) bleibt **unverändert app-managed**. Der MCP-Endpoint wird für v1 deaktiviert.

**Tech Stack:** Python 3.12, FastAPI, Starlette SessionMiddleware (bleibt für CSRF/Flash), SQLAlchemy 2 + Alembic, SQLite (WAL), Jinja2 (server-rendered), Docker, Forgejo Actions (mindcode).

**Decisions locked in (Brainstorming 2026-06-26):**
- DB: **SQLite im Volume**, vorhandene Postgres-Daten verlustfrei migrieren, WAL + `busy_timeout` + Backup.
- Auth: **voll auf `X-MSQ`**; Passwort- und M365-SSO-Login raus; Admin via Email-Allowlist (`ADMIN_EMAILS`) und optional `AppHub.Admin`-Rolle.
- Salesforce: **komplett app-managed behalten** (Service-User + per-User-OAuth + Sync). Hub-SF-Capability erst, wenn SF-App-Berechtigungen existieren — nicht Teil dieser Migration.
- Registry/Repo: **komplett nach `mindcode.mindsquare.de`** (Code + Image), CI auf Forgejo Actions.
- MCP: **bleibt aktiv** und authentifiziert künftig über M365/Entra via Hub-`mcp-bearer` (kein App-Token mehr). Der MCP-Server liest die Identität aus `X-MSQ-*`, `/timehub/mcp` ist erreichbar.
- Backup/Restore: **self-service über die TimeHub-GUI** (Admin-only ZIP-Download/Upload) — auch der Initial-Datenimport läuft so, ohne Hub-Admin/Volume-Zugriff.

**Open coordination items (mit Hub-Admin / vorab zu klären — blockieren die Implementierung NICHT, aber das Go-Live):**
1. **Git-Host-Credentials** für `mindcode.mindsquare.de` im Hub hinterlegt (Pull-Zugriff aufs Package).
2. **`auth_mode=mcp-bearer`** für die App im Hub setzen, damit der Hub die M365/Entra-OAuth für `/timehub/mcp` übernimmt (`/.well-known/oauth-protected-resource/timehub` ist hub-seitig).
3. **Initial-Datenimport ohne Hub-Admin:** Die migrierte DB wird als ZIP **über die TimeHub-GUI** eingespielt (Admin → Restore, Task 4) — kein `docker cp`/Volume-Zugriff nötig.
4. **Sub-Path:** Slug ist `/timehub` (bestätigt). Wir machen uns über `BASE_PATH=/timehub` unabhängig davon, ob der Hub `X-Forwarded-Prefix` schickt.
5. **`SECRET_KEY`** als Secret-Env im Hub: signiert das Starlette-**Session-Cookie**, das TimeHub weiterhin für **CSRF-Tokens + Flash-Messages** nutzt (nicht mehr für Login/JWT). Ohne ihn brechen Formular-POSTs an der CSRF-Prüfung. 48-Byte-Zufallswert.

---

## File Structure

**Neu:**
- `app/identity.py` — Per-Request-Identität: `X-MSQ-*` → TimeHub-User, Dev-Bypass, Admin-Mapping. **Kein globales BaseHTTPMiddleware** — Auflösung lazy in den Readern (würde sonst den MCP-SSE-Stream brechen, siehe `app/scope_mw.py`).
- `app/web/templating.py` — `BASE_PATH`-bewusster `path()`-Jinja-Helper + `join_base()` für Redirects.
- `app/services/backup.py` — konsistenter SQLite-Snapshot (`VACUUM INTO`) + Restore über die sqlite3-Backup-API.
- `scripts/migrate_pg_to_sqlite.py` — Einmal-Datenmigration Postgres → SQLite (Output wird als Restore-ZIP über die GUI eingespielt).
- `alembic/versions/<rev>_add_msq_user_id.py` — neue Spalte `users.msq_user_id`.
- `.forgejo/workflows/build.yml` — Build+Push nach `mindcode.mindsquare.de`.
- `tests/test_identity.py`, `tests/test_base_path.py`, `tests/test_pg_to_sqlite_migration.py`, `tests/test_backup_restore.py`.

**Geändert:**
- `app/db.py` — SQLite WAL/`busy_timeout` via `event.listen`.
- `app/config.py` — neue Settings (`auth_mode`, `admin_emails`, `base_path`), `mcp_enabled` bleibt `True`, SQLite-Default-Pfad.
- `app/deps.py` — `get_current_user` löst Hub-Identität lazy auf (`resolve_request_user`).
- `app/web/common.py` — `_maybe_user` löst Hub-Identität lazy auf; `path`-Jinja-Global.
- `app/main.py` — `root_path`, `/health`-Alias, LoginRequired→401 statt /login-Redirect, pure-ASGI HTML-No-Cache.
- `app/mcp_server.py` — `ApiKeyAuthMiddleware` → X-MSQ-Identity (mcp-bearer), pure-ASGI bleibt.
- `app/web/routes/admin.py` — Backup/Restore-Endpunkte (admin-only).
- `app/web/router.py` — `m365_login` aus dem Include entfernen.
- `app/web/routes/account.py` — Passwort-`/login`-GET/POST raus, `/logout` → Hub delegieren.
- `app/api/auth.py` — `POST /auth/login` (Passwort) raus; Router-Prefix von `/auth` auf `/account-api` (reservierter Pfad).
- `app/api/__init__.py` — falls Prefix-Änderung dort verdrahtet.
- `app/web/templates/base.html` — Waffle-Script, Logout→`/auth/logout`, Login-Link raus, absolute Pfade über `path()`.
- `app/web/templates/*.html` — absolute App-interne URLs über `path()`.
- `app/models/user.py` — Spalte `msq_user_id`.
- `app/services/bootstrap.py` — `ensure_initial_admin` durch Allowlist-Logik ersetzt/ergänzt.
- `Dockerfile` — OCI/Hub-Labels, `COPY .env.example`, `/health`, `VOLUME`.
- `.env.example` — Hub-Variablen, obsolete raus.
- `README.md` — Container-Sektion (mindcode), Navigation-Override-Hinweis.
- `docker-compose.yml` / `docker-compose.prod.yml` — auf lokalen Dev-Modus reduzieren (kein Host-Port-Binding im Hub-Pfad); prod-Compose entfällt fürs Hub-Deployment.
- `.github/workflows/*` — entfernen oder als deaktivierten Altbestand markieren.
- `tests/conftest.py` — Test-Client primt Identität über X-MSQ-Header statt Passwort-Login.

**Entfernt:**
- `app/web/routes/m365_login.py` (Hub macht SSO).
- `app/web/templates/login.html` (kein eigener Login mehr).

---

## Phase 1 — SQLite tragfähig machen + Daten migrieren

### Task 1: SQLite WAL + busy_timeout

**Files:**
- Modify: `app/db.py`
- Test: `tests/test_sqlite_pragmas.py` (Create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sqlite_pragmas.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sqlite_pragmas.py -v`
Expected: FAIL (journal_mode is `memory`/`delete`, busy_timeout 0, foreign_keys 0).

- [ ] **Step 3: Implement the pragma listener**

```python
# app/db.py — replace _make_engine() and add the listener
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine() -> Engine:
    settings = get_settings()
    url = settings.database_url
    connect_args: dict = {}
    if url.startswith("sqlite"):
        # check_same_thread off: FastAPI hands the connection across threads.
        # timeout: block (not error) up to 30s when another writer holds the lock.
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = 30
    return create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)


engine = _make_engine()


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    # Only SQLite needs these; Postgres connections are left untouched.
    if engine.url.drivername.startswith("sqlite"):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")     # concurrent readers + one writer
        cur.execute("PRAGMA busy_timeout=30000")   # wait instead of "database is locked"
        cur.execute("PRAGMA foreign_keys=ON")      # enforce FKs (off by default in SQLite)
        cur.execute("PRAGMA synchronous=NORMAL")   # safe + fast under WAL
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sqlite_pragmas.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_sqlite_pragmas.py
git commit -m "feat(db): enable SQLite WAL, busy_timeout and FK enforcement"
```

---

### Task 2: SQLite-Default-Pfad + VOLUME

**Files:**
- Modify: `app/config.py:79-81` (`_resolve_database_url`)
- Modify: `Dockerfile` (VOLUME, data dir)
- Test: `tests/test_database_url.py` (existing — extend)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_database_url.py — add
def test_sqlite_default_lives_under_app_data(monkeypatch):
    from app.config import Settings

    for var in ("DATABASE_URL", "POSTGRES_USER", "POSTGRES_PASSWORD",
                "POSTGRES_HOST", "POSTGRES_DB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    s = Settings()
    assert s.database_url == "sqlite:////app/data/timehub.sqlite"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_database_url.py -k sqlite_default -v`
Expected: FAIL (current default is relative `sqlite:///./data/timehub.sqlite`).

- [ ] **Step 3: Implement absolute container path**

In `app/config.py`, change the SQLite fallback in `_resolve_database_url`:

```python
        else:
            # Absolute path inside the container so the named Hub volume
            # (mounted at /app/data) always holds the DB, regardless of CWD.
            self.database_url = "sqlite:////app/data/timehub.sqlite"
        return self
```

- [ ] **Step 4: Add VOLUME + data dir to Dockerfile**

In `Dockerfile`, after the `mkdir -p /app/data /app/uploads` line, add:

```dockerfile
VOLUME ["/app/data", "/app/uploads"]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_database_url.py -v`
Expected: PASS (other DB-URL tests unaffected — they set env explicitly).

- [ ] **Step 6: Commit**

```bash
git add app/config.py Dockerfile tests/test_database_url.py
git commit -m "feat(db): default SQLite to /app/data volume and declare VOLUME"
```

---

### Task 3: Postgres → SQLite Datenmigration (Einmal-Skript)

**Files:**
- Create: `scripts/migrate_pg_to_sqlite.py`
- Test: `tests/test_pg_to_sqlite_migration.py`

**Approach:** Quelle (Postgres) und Ziel (SQLite) über getrennte SQLAlchemy-Engines. Ziel-Schema via `Base.metadata.create_all` (Alembic-Stempel danach). Tabellen in FK-Reihenfolge kopieren (`Base.metadata.sorted_tables` liefert genau das). Pro Tabelle Zeilen blockweise lesen und in die Ziel-Engine schreiben. Identity-Sequenzen sind in SQLite irrelevant (AUTOINCREMENT folgt dem höchsten Wert).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pg_to_sqlite_migration.py
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

    dst_file = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    dst = create_engine(f"sqlite:///{dst_file.name}")

    copy_all(src, dst)

    with dst.connect() as conn:
        rows = conn.execute(select(User.email).order_by(User.id)).scalars().all()
    assert rows == ["a@x.de", "b@x.de"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pg_to_sqlite_migration.py -v`
Expected: FAIL (`ModuleNotFoundError: scripts.migrate_pg_to_sqlite`).

- [ ] **Step 3: Implement the migration script**

```python
# scripts/migrate_pg_to_sqlite.py
"""One-time data migration: copy every row from the production Postgres into a
fresh SQLite file, preserving primary keys and foreign-key order.

Usage:
    SOURCE_URL=postgresql+psycopg://timehub:***@host:5432/timehub \
    TARGET_URL=sqlite:////absolute/path/timehub.sqlite \
    python -m scripts.migrate_pg_to_sqlite

The target schema is created from the models, so run this against the SAME code
revision the Hub image will run. After it finishes, stamp Alembic to head:
    DATABASE_URL=$TARGET_URL alembic stamp head
"""

import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app import models  # noqa: F401 — registers all tables on Base.metadata
from app.db import Base

_BATCH = 1000


def copy_all(source: Engine, target: Engine) -> None:
    Base.metadata.create_all(target)
    # sorted_tables is parent-before-child → satisfies FK constraints on insert.
    for table in Base.metadata.sorted_tables:
        with source.connect() as src_conn:
            rows = [dict(r._mapping) for r in src_conn.execute(table.select())]
        if not rows:
            print(f"  {table.name}: 0 rows")
            continue
        with target.begin() as dst_conn:
            for start in range(0, len(rows), _BATCH):
                dst_conn.execute(table.insert(), rows[start : start + _BATCH])
        print(f"  {table.name}: {len(rows)} rows")


def main() -> int:
    source_url = os.environ.get("SOURCE_URL")
    target_url = os.environ.get("TARGET_URL")
    if not source_url or not target_url:
        print("Set SOURCE_URL (Postgres) and TARGET_URL (sqlite:///...).", file=sys.stderr)
        return 2
    print(f"Migrating {source_url} → {target_url}")
    copy_all(create_engine(source_url, future=True), create_engine(target_url, future=True))
    print("Done. Now run: DATABASE_URL=$TARGET_URL alembic stamp head")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pg_to_sqlite_migration.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_pg_to_sqlite.py tests/test_pg_to_sqlite_migration.py
git commit -m "feat(migration): add Postgres-to-SQLite data copy script"
```

---

### Task 4: Backup/Restore per ZIP über die GUI (admin-only)

**Files:**
- Create: `app/services/backup.py`
- Modify: `app/db.py` (export `sqlite_path()` helper — or keep it in backup.py)
- Modify: `app/web/routes/admin.py` (two endpoints), `app/web/templates/users.html` (Backup/Restore-Abschnitt)
- Test: `tests/test_backup_restore.py`

**Approach:** Self-service statt Hub-Admin. **Backup** macht mit `VACUUM INTO` einen WAL-konsistenten Snapshot und packt ihn als `db/timehub.sqlite` (+ optional `uploads/`) in ein ZIP. **Restore** validiert das ZIP (`integrity_check` + Pflichttabellen) und überschreibt die Live-DB über die sqlite3-Online-Backup-API (sauberes Locking). **Dieselbe Restore-Funktion ist auch der Initial-Datenimport:** nach dem ersten Deploy lädt der Admin die mit Task 3 migrierte DB als ZIP hoch. Überschreibt **alle** Daten → UI-Bestätigung + Hinweis „danach neu laden".

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backup_restore.py
import io
import sqlite3
import zipfile

import pytest

from app.services.backup import make_backup_zip, restore_from_zip


def test_backup_zip_contains_db(client):
    # client = admin (dev user). The backup endpoint streams a zip.
    r = client.get("/admin/backup")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert "db/timehub.sqlite" in zf.namelist()


def test_restore_roundtrip(client):
    data = client.get("/admin/backup").content
    # Restoring the app's own backup must succeed and keep the schema intact.
    restore_from_zip(data, uploads_dir=None)
    from app.db import engine

    with engine.connect() as conn:
        from sqlalchemy import text

        assert conn.execute(text("SELECT count(*) FROM users")).scalar() >= 1


def test_restore_rejects_non_timehub_db():
    # A valid SQLite file without the expected tables must be refused.
    buf = io.BytesIO()
    bad = sqlite3.connect(":memory:")
    # serialize an empty db into the zip
    import tempfile, os

    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    sqlite3.connect(tmp.name).execute("CREATE TABLE foo (x INTEGER)")
    with zipfile.ZipFile(buf, "w") as zf:
        zf.write(tmp.name, "db/timehub.sqlite")
    os.unlink(tmp.name)
    with pytest.raises(ValueError):
        restore_from_zip(buf.getvalue(), uploads_dir=None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backup_restore.py -v`
Expected: FAIL (`ModuleNotFoundError: app.services.backup`).

- [ ] **Step 3: Implement app/services/backup.py**

```python
# app/services/backup.py
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

    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
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
```

- [ ] **Step 4: Add the admin endpoints**

In `app/web/routes/admin.py` add (import `zipfile`, `Response`, `UploadFile`, `File`, and `from app.services import backup as backup_svc`, `from app.web.templating import join_base`):

```python
@router.get("/admin/backup")
def admin_backup(request: Request, db: Session = Depends(get_db)) -> Response:
    _require_admin(request, db)
    data = backup_svc.make_backup_zip(uploads_dir="/app/uploads")
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="timehub-backup.zip"'},
    )


@router.post("/admin/restore")
async def admin_restore(
    request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)
) -> RedirectResponse:
    _require_admin(request, db)
    raw = await file.read()
    base = request.scope.get("root_path", "")
    try:
        backup_svc.restore_from_zip(raw, uploads_dir="/app/uploads")
    except (ValueError, zipfile.BadZipFile) as exc:
        return RedirectResponse(
            url=join_base(base, f"/users?error={exc}"), status_code=303
        )
    return RedirectResponse(
        url=join_base(base, "/users?flash=Wiederherstellung+erfolgreich+–+bitte+neu+laden"),
        status_code=303,
    )
```

- [ ] **Step 5: Add the UI section to users.html**

Under the admin settings, add a "Datensicherung" card: a download link to `{{ path('/admin/backup') }}` and a `<form method="post" enctype="multipart/form-data" action="{{ path('/admin/restore') }}">` with a file input, the CSRF hidden field, and a confirm-dialog (`onsubmit="return confirm('Überschreibt ALLE Daten. Fortfahren?')"`).

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_backup_restore.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/services/backup.py app/web/routes/admin.py app/web/templates/users.html tests/test_backup_restore.py
git commit -m "feat(admin): self-service SQLite backup/restore via ZIP from the GUI"
```

> **Initial-Import (Runbook):** Nach dem ersten Hub-Deploy: Task 3 lokal gegen das Prod-Postgres laufen lassen → `timehub.sqlite`, in ein ZIP mit `db/timehub.sqlite` packen, als Admin über **Datensicherung → Wiederherstellen** hochladen, danach Seite neu laden.

---

## Phase 2 — Hub-Identität (`X-MSQ-*`)

### Task 5: Settings für Identität + Admin-Allowlist

**Files:**
- Modify: `app/config.py`
- Test: `tests/test_identity_settings.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_identity_settings.py
def test_auth_mode_defaults_closed_in_production(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.delenv("AUTH_MODE", raising=False)
    assert Settings().resolved_auth_mode == "hub"


def test_auth_mode_defaults_open_outside_production(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("AUTH_MODE", raising=False)
    assert Settings().resolved_auth_mode == "dev-bypass"


def test_admin_emails_parse_and_lowercase(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ADMIN_EMAILS", "Rick@mindsquare.de, boss@x.de")
    assert Settings().admin_email_set == {"rick@mindsquare.de", "boss@x.de"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_identity_settings.py -v`
Expected: FAIL (`resolved_auth_mode`/`admin_email_set` don't exist).

- [ ] **Step 3: Add the settings**

In `app/config.py`, add fields to `Settings` and two helpers:

```python
    # --- Agent Hub identity ---
    # "hub": trust X-MSQ-* headers (production behind the Hub).
    # "dev-bypass": inject a fixed local dev user (no Hub in front).
    # Empty → resolved by APP_ENV (prod=hub, else dev-bypass).
    auth_mode: str | None = None
    # Comma-separated emails that become TimeHub admins on provision/login.
    admin_emails: str = ""
    # Mount path the Hub serves the app under (e.g. "/timehub"); "" for root.
    base_path: str = ""
    # Dev-bypass identity (only used when auth_mode resolves to dev-bypass).
    dev_user_email: str = "dev@mindsquare.local"
    dev_user_name: str = "Dev User"
    dev_user_admin: bool = True
```

Add the helpers (after `cors_origin_list`):

```python
    @property
    def resolved_auth_mode(self) -> str:
        raw = (self.auth_mode or "").strip().lower()
        if raw in {"hub", "dev-bypass"}:
            return raw
        return "hub" if self.app_env.strip().lower() == "production" else "dev-bypass"

    @property
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def normalized_base_path(self) -> str:
        bp = "/" + self.base_path.strip().strip("/")
        return "" if bp == "/" else bp
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_identity_settings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_identity_settings.py
git commit -m "feat(config): add Hub auth_mode, admin allowlist and base_path settings"
```

---

### Task 6: User-Spalte `msq_user_id` + Alembic-Migration

**Files:**
- Modify: `app/models/user.py`
- Create: `alembic/versions/<rev>_add_msq_user_id.py`
- Test: `tests/test_migrations.py` (existing — runs upgrade head; extend with a column check)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migrations.py — add
def test_users_table_has_msq_user_id():
    from sqlalchemy import inspect

    from app.db import engine

    cols = {c["name"] for c in inspect(engine).get_columns("users")}
    assert "msq_user_id" in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_migrations.py -k msq_user_id -v`
Expected: FAIL (column missing).

- [ ] **Step 3: Add the model column**

In `app/models/user.py`, after the `entra_oid` column:

```python
    # Stable Hub subject id from X-MSQ-User-Id. Opaque — do NOT assume it equals
    # the Entra oid (contract A.1.4). Primary match key behind the Agent Hub.
    msq_user_id: Mapped[str | None] = mapped_column(
        String(128), unique=True, index=True, nullable=True
    )
```

- [ ] **Step 4: Generate + edit the migration**

Run: `alembic revision -m "add msq_user_id"` then set its body:

```python
def upgrade() -> None:
    op.add_column("users", sa.Column("msq_user_id", sa.String(length=128), nullable=True))
    op.create_index("ix_users_msq_user_id", "users", ["msq_user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_msq_user_id", table_name="users")
    op.drop_column("users", "msq_user_id")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_migrations.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/models/user.py alembic/versions/
git commit -m "feat(model): add users.msq_user_id for Hub identity mapping"
```

---

### Task 7: Identität — Resolver + Per-Request-Auflösung (kein globales Middleware)

**Files:**
- Create: `app/identity.py`
- Test: `tests/test_identity.py`

**Behaviour:** **Kein** globales `BaseHTTPMiddleware` (das würde den MCP-SSE-Stream puffern/brechen — der Codebase nutzt bewusst pure-ASGI, siehe `app/scope_mw.py`). Stattdessen löst ein lazy Resolver pro Request die Identität auf, sobald ein Reader sie braucht, und cacht das Ergebnis auf `request.state.hub_user_id`. In `dev-bypass`-Mode wird die Dev-Identität injiziert; in `hub`-Mode kommt sie aus `X-MSQ-*` (fehlt `X-MSQ-User-Id` → kein User → Reader liefert 401). Der Resolver matcht/provisioniert den User und setzt Admin aus Allowlist **oder** `AppHub.Admin`-Rolle.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_identity.py
from app.identity import HubPrincipal, resolve_user
from app.db import SessionLocal


def _principal(**kw):
    base = dict(subject="msq-1", email="new@mindsquare.de", name="New User",
                roles=frozenset(), guest=False)
    base.update(kw)
    return HubPrincipal(**base)


def test_resolve_provisions_unknown_user():
    with SessionLocal() as db:
        u = resolve_user(db, _principal(subject="msq-prov-1", email="prov1@x.de"))
        assert u.id is not None
        assert u.msq_user_id == "msq-prov-1"
        assert u.is_admin is False


def test_admin_email_allowlist_grants_admin(monkeypatch):
    monkeypatch.setattr(
        "app.identity.get_settings",
        lambda: _settings_with(admin_emails={"chief@mindsquare.de"}),
    )
    with SessionLocal() as db:
        u = resolve_user(db, _principal(subject="msq-admin-1", email="chief@mindsquare.de"))
        assert u.is_admin is True


def test_apphub_admin_role_grants_admin():
    with SessionLocal() as db:
        u = resolve_user(
            db, _principal(subject="msq-admin-2", email="ops@x.de",
                           roles=frozenset({"AppHub.Admin"}))
        )
        assert u.is_admin is True


def test_existing_user_matched_by_email_and_backfilled():
    with SessionLocal() as db:
        from app.models import User

        db.add(User(email="legacy@x.de", full_name="Legacy", is_active=True))
        db.commit()
        u = resolve_user(db, _principal(subject="msq-legacy-1", email="legacy@x.de"))
        assert u.msq_user_id == "msq-legacy-1"  # backfilled, no duplicate row


def _settings_with(admin_emails):
    class _S:
        admin_email_set = admin_emails
    return _S()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_identity.py -v`
Expected: FAIL (`ModuleNotFoundError: app.identity`).

- [ ] **Step 3: Implement app/identity.py**

```python
# app/identity.py
"""Agent Hub identity: read X-MSQ-* headers, resolve to a TimeHub user.

Replaces the app's own password/SSO login. The Hub strips inbound X-MSQ-* and
re-sets them from a validated session, so any header we receive is trusted
(contract A.1.4). Missing X-MSQ-User-Id in hub mode = request did not come
through the Hub → unauthenticated.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import get_settings
from app.db import SessionLocal
from app.models import User

log = logging.getLogger(__name__)

ADMIN_ROLE = "AppHub.Admin"


@dataclass(frozen=True)
class HubPrincipal:
    subject: str            # X-MSQ-User-Id (stable, opaque)
    email: str | None
    name: str | None
    roles: frozenset[str]
    guest: bool


def principal_from_headers(headers) -> HubPrincipal | None:
    subject = headers.get("x-msq-user-id")
    if not subject:
        return None
    roles = frozenset(
        r.strip() for r in (headers.get("x-msq-roles") or "").split(",") if r.strip()
    )
    return HubPrincipal(
        subject=subject,
        email=(headers.get("x-msq-user-email") or "").lower() or None,
        name=headers.get("x-msq-user-name") or None,
        roles=roles,
        guest=(headers.get("x-msq-guest") == "true"),
    )


def _dev_principal() -> HubPrincipal:
    s = get_settings()
    return HubPrincipal(
        subject="dev-local",
        email=s.dev_user_email.lower(),
        name=s.dev_user_name,
        roles=frozenset({ADMIN_ROLE}) if s.dev_user_admin else frozenset(),
        guest=False,
    )


def _should_be_admin(principal: HubPrincipal) -> bool:
    if ADMIN_ROLE in principal.roles:
        return True
    return bool(principal.email and principal.email in get_settings().admin_email_set)


def resolve_user(db: Session, principal: HubPrincipal) -> User:
    """Match by msq_user_id, then email; provision if unknown. Admin status is
    re-evaluated on every login so allowlist/role changes take effect."""
    user = db.execute(
        select(User).where(User.msq_user_id == principal.subject)
    ).scalar_one_or_none()
    if user is None and principal.email:
        user = db.execute(
            select(User).where(func.lower(User.email) == principal.email)
        ).scalar_one_or_none()

    if user is None:
        user = User(
            email=principal.email or f"{principal.subject}@hub.local",
            full_name=principal.name or principal.email or principal.subject,
            msq_user_id=principal.subject,
            is_active=True,
            is_admin=_should_be_admin(principal),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        log.info("Provisioned TimeHub user from Hub identity: %s", user.email)
        return user

    changed = False
    if user.msq_user_id != principal.subject:
        user.msq_user_id = principal.subject
        changed = True
    admin = _should_be_admin(principal)
    if admin and not user.is_admin:
        user.is_admin = True
        changed = True
    if principal.name and user.full_name != principal.name:
        user.full_name = principal.name
        changed = True
    if changed:
        db.add(user)
        db.commit()
    return user


def principal_for_request(request: Request) -> HubPrincipal | None:
    """Dev-bypass injects a fixed identity; otherwise read the Hub headers."""
    if get_settings().resolved_auth_mode == "dev-bypass":
        return _dev_principal()
    return principal_from_headers(request.headers)


def resolve_request_user(request: Request, db: Session) -> User | None:
    """Resolve (and cache on request.state) the TimeHub user for this request.
    Returns None when no Hub identity is present (→ caller raises 401/redirect).
    Called lazily by the web/API readers — no global middleware, so the MCP
    SSE stream is never wrapped by BaseHTTPMiddleware."""
    cached = getattr(request.state, "hub_user_id", None)
    if cached is not None:
        return db.get(User, cached)
    principal = principal_for_request(request)
    if principal is None:
        return None
    user = resolve_user(db, principal)
    request.state.hub_user_id = user.id
    request.state.hub_is_guest = principal.guest
    return user
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_identity.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/identity.py tests/test_identity.py
git commit -m "feat(auth): add Hub identity resolver (lazy, no global middleware)"
```

---

### Task 8: Web/API-Reader auf Hub-Identität umstellen

**Files:**
- Modify: `app/web/common.py:134-145` (`_maybe_user`)
- Modify: `app/deps.py:81-113` (`get_current_user`)
- Modify: `app/main.py` (register middleware; LoginRequired → 401)
- Test: `tests/test_identity_wiring.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_identity_wiring.py
def test_web_page_uses_hub_header(client):
    # dev-bypass is on in tests → a normal GET already carries identity.
    r = client.get("/")
    assert r.status_code == 200


def test_hub_mode_without_header_is_unauthenticated(monkeypatch, client):
    monkeypatch.setenv("AUTH_MODE", "hub")
    from app.config import get_settings

    get_settings.cache_clear()
    r = client.get("/", headers={})  # no X-MSQ-User-Id
    assert r.status_code in (401, 403)
    get_settings.cache_clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_identity_wiring.py -v`
Expected: FAIL (readers still use session token; no middleware).

- [ ] **Step 3: Rewrite `_maybe_user` to resolve Hub identity lazily**

In `app/web/common.py`, replace `_maybe_user` (the `decode_token` import inside it goes away):

```python
def _maybe_user(request: Request, db: Session) -> User | None:
    """Identity comes from the Hub (X-MSQ-*) or dev-bypass, resolved lazily and
    cached on request.state. No session-token decoding."""
    from app.identity import resolve_request_user

    return resolve_request_user(request, db)
```

- [ ] **Step 4: Rewrite `get_current_user` (API) to use the Hub identity**

In `app/deps.py`, replace the body of `get_current_user`. The API-key path stays only as a non-Hub fallback (inert behind the Hub, since the Hub forwards no API-key requests):

```python
def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    x_api_key: str | None = Header(default=None),
) -> User:
    from app.identity import resolve_request_user

    user = resolve_request_user(request, db)
    if user is not None:
        request.state.api_scope = "read_write"

    if user is None and x_api_key:
        result = _api_key_auth(x_api_key.strip(), db)
        if result is not None:
            user, scope = result
            request.state.api_scope = scope

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user
```

Remove the now-unused `authorization`/`_user_from_bearer` wiring only if nothing else references them (verify with `grep -rn _user_from_bearer app`).

- [ ] **Step 5: `app/main.py` — root_path, LoginRequired → 401 (no middleware registration)**

There is **no** global identity middleware (the readers resolve lazily). Two changes:

1. Set `root_path` on the app so url_for/OpenAPI carry the slug. Add to the `FastAPI(...)` constructor:

```python
app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="Zentrale Zeiterfassung – API für Erfassung, Import, Export und Reporting.",
    lifespan=lifespan,
    root_path=settings.normalized_base_path,
)
```

2. Behind the Hub there is no `/login`; an unauthenticated web request means the Hub didn't forward identity → answer 401 (the Hub renders its own login):

```python
@app.exception_handler(LoginRequired)
async def _login_required_handler(request, exc):
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": "Not authenticated"}, status_code=401)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_identity_wiring.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/web/common.py app/deps.py app/main.py tests/test_identity_wiring.py
git commit -m "feat(auth): resolve Hub identity in web and API readers; 401 when absent"
```

---

### Task 9: Eigenes Login/SSO entfernen

**Files:**
- Modify: `app/web/routes/account.py` (remove `/login` GET+POST, repoint `/logout`)
- Delete: `app/web/routes/m365_login.py`
- Modify: `app/web/router.py` (drop `m365_login` import + include)
- Modify: `app/api/auth.py` (remove password `POST /auth/login`; change prefix `/auth` → `/account-api`)
- Delete: `app/web/templates/login.html`
- Test: `tests/test_login_removed.py`; update `tests/test_m365_sso.py`, `tests/test_profile_api_keys.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_login_removed.py
def test_password_login_endpoints_are_gone(client):
    # GET /login no longer renders a form (404, route removed).
    assert client.get("/login").status_code == 404
    assert client.post("/auth/login", json={"email": "a", "password": "b"}).status_code == 404


def test_m365_sso_login_route_gone(client):
    assert client.get("/auth/m365/login", follow_redirects=False).status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_login_removed.py -v`
Expected: FAIL (routes still exist).

- [ ] **Step 3: Remove the routes**

- In `app/web/routes/account.py`: delete `login_form` (`GET /login`) and `login_submit` (`POST /login`). Change `logout` to delegate to the Hub:

```python
@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    # The Hub owns the session; /auth/logout ends the SSO session hub-wide.
    return RedirectResponse(url="/auth/logout", status_code=status.HTTP_302_FOUND)
```

- Delete `app/web/routes/m365_login.py`.
- In `app/web/router.py`: remove `m365_login` from both the import tuple and the include loop.
- In `app/api/auth.py`: delete the `login` function and the now-unused imports (`LoginRequest`, `TokenResponse`, `verify_password`, `create_access_token` — verify each with grep before removing). Change the router line to:

```python
router = APIRouter(prefix="/account-api", tags=["account"])
```

- Delete `app/web/templates/login.html`.

- [ ] **Step 4: Fix the dependent tests**

- `tests/test_m365_sso.py`: this exercised the removed SSO-login flow — replace its assertions with Task 7/8 identity coverage or delete the file if fully superseded (it is). Delete it.
- `tests/test_profile_api_keys.py`: update any `/auth/api-keys` URL to `/account-api/api-keys`.
- Any test calling `POST /auth/login` to authenticate must switch to the X-MSQ header / dev-bypass path (see Task 10 conftest change).

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_login_removed.py tests/test_profile_api_keys.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A app/web/routes app/web/router.py app/api/auth.py app/web/templates tests/
git commit -m "feat(auth): remove password login and M365 SSO; logout delegates to Hub"
```

---

### Task 10: Test-Harness auf Hub-Identität umstellen

**Files:**
- Modify: `tests/conftest.py`
- Modify: `app/services/bootstrap.py` (drop password-admin bootstrap; keep format seeding)
- Test: full suite green

- [ ] **Step 1: Update conftest to prime identity via dev-bypass + header**

```python
# tests/conftest.py — relevant parts
import os
import re
import tempfile

import pytest

os.environ.setdefault("APP_ENV", "test")          # → resolved_auth_mode = dev-bypass
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_EMAILS", "dev@mindsquare.local")  # dev user is admin

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)  # noqa: SIM115
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}"


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    from app import models  # noqa: F401
    from app.db import Base, engine

    Base.metadata.create_all(engine)
    from app.services.bootstrap import ensure_builtin_formats

    ensure_builtin_formats()
    yield


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        # dev-bypass injects identity, so the first GET also provisions the dev
        # user and mints a CSRF token. Lift the token for unsafe requests.
        html = c.get("/").text
        m = re.search(r'name="csrf-token" content="([^"]+)"', html)
        if m:
            c.headers["X-CSRF-Token"] = m.group(1)
        yield c
```

- [ ] **Step 2: Simplify bootstrap.py**

Remove `ensure_initial_admin` (admin now comes from the allowlist on first Hub login). Drop its import from `app/main.py` lifespan. Keep `ensure_builtin_formats`.

- [ ] **Step 3: Run the full suite**

Run: `pytest -q`
Expected: PASS (fix any test that assumed a second non-dev user could log in via password — switch such tests to set `X-MSQ-User-Id`/`X-MSQ-User-Email` headers explicitly and `AUTH_MODE=hub` for that case).

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py app/services/bootstrap.py app/main.py
git commit -m "test: prime identity via dev-bypass; drop password-admin bootstrap"
```

---

## Phase 3 — Sub-Path / strip_prefix + Contract-Endpoints

### Task 11: BASE_PATH-bewusste URLs, `/health`, Waffle, Self-Framing

**Files:**
- Create: `app/web/templating.py`
- Modify: `app/main.py` (`/health` alias, X-Frame self-framing already SAMEORIGIN by Hub — no app CSP change needed unless app adds one)
- Modify: `app/web/common.py` (register `path` Jinja global; SPA-style cache header for HTML)
- Modify: `app/web/templates/base.html` and all templates using absolute app URLs
- Test: `tests/test_base_path.py`

**Strategy:** The Hub serves the app at `/<slug>/` with `strip_prefix=true`, so the app receives requests at `/`. Browser-visible URLs must include the slug. We set FastAPI `root_path` (Task 8) and add a Jinja `path("/x")` helper that prepends `request.scope["root_path"]`. All app-internal absolute URLs in templates and `RedirectResponse` go through it. Asset (`/static/*`) and the `index`-equivalent HTML use the cache rules from contract A.2.7.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_base_path.py
from app.web.templating import join_base


def test_join_base_prefixes_root_path():
    assert join_base("/timehub", "/static/app.js") == "/timehub/static/app.js"
    assert join_base("", "/static/app.js") == "/static/app.js"
    assert join_base("/timehub", "static/app.js") == "/timehub/static/app.js"


def test_health_endpoint_no_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_base_path.py -v`
Expected: FAIL (`app.web.templating` missing; `/health` 404).

- [ ] **Step 3: Implement the helper**

```python
# app/web/templating.py
"""Sub-path helper: the Agent Hub serves the app under /<slug>/ with
strip_prefix, so the app sees "/" but browser URLs must carry the prefix.
root_path (set on the FastAPI app) holds that prefix; join_base prepends it."""


def join_base(root_path: str, path: str) -> str:
    base = (root_path or "").rstrip("/")
    suffix = path if path.startswith("/") else "/" + path
    return f"{base}{suffix}"
```

- [ ] **Step 4: Register a Jinja global in `app/web/common.py`**

After `templates = Jinja2Templates(...)`:

```python
from app.web.templating import join_base


def _template_path(context, path: str) -> str:
    request = context["request"]
    return join_base(request.scope.get("root_path", ""), path)


templates.env.globals["path"] = _template_path
templates.env.globals["pass_context_path"] = True
```

Note: use `@jinja2.pass_context` semantics — register as:

```python
import jinja2


@jinja2.pass_context
def _template_path(context, path: str) -> str:
    request = context["request"]
    return join_base(request.scope.get("root_path", ""), path)


templates.env.globals["path"] = _template_path
```

- [ ] **Step 5: Add `/health` in `app/main.py`**

```python
@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok"}
```

(Keep `/healthz` and `/readyz` for backward compatibility.)

- [ ] **Step 6: Sweep templates**

In `app/web/templates/base.html` and the others flagged by the grep below, replace app-internal absolute URLs:
- `href="/static/icon.svg"` → `href="{{ path('/static/icon.svg') }}"`
- `action="/logout"` → `action="{{ path('/logout') }}"`
- nav `href="/..."` → `href="{{ path('/...') }}"`
- remove the `<a href="/login">Login</a>` nav item entirely.

External URLs (fonts, cdn.tailwindcss.com) stay untouched. Find the sites:

```bash
grep -rn 'href="/\|action="/\|src="/\|url("/\|fetch("/\|fetch(`/' app/web/templates app/web/static 2>/dev/null
```

For JS `fetch("/...")` calls in static JS that can't read Jinja, expose the base path once in `base.html`:

```html
<meta name="base-path" content="{{ path('') }}" />
```

and prefix fetches in JS with that value (helper in the app's main JS).

- [ ] **Step 7: Add the Hub Waffle to base.html**

In `<head>` (or end of `<body>`):

```html
<script src="/embed/waffle.js" defer></script>
```

(Absolute, served by the Hub at domain root — **not** through `path()`.)

- [ ] **Step 8: SPA-style cache header for HTML responses**

TimeHub is server-rendered (not a Vite SPA), so the strict A.2.7 rule is softened, but HTML must not be heuristically cached. Use a **pure-ASGI** middleware (not `BaseHTTPMiddleware`) so it only touches the response-start headers and never buffers the body — keeping the MCP SSE stream intact, consistent with `app/scope_mw.py`:

```python
# app/main.py
from starlette.datastructures import MutableHeaders


class HtmlNoCacheASGI:
    """Append Cache-Control: no-cache to text/html responses. Pure ASGI: it
    inspects only the http.response.start message and passes body frames
    through untouched (safe for streaming/SSE)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                if headers.get("content-type", "").startswith("text/html"):
                    headers["Cache-Control"] = "no-cache, must-revalidate"
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(HtmlNoCacheASGI)
```

Static assets under `/static` keep StaticFiles defaults (acceptable; tighten to `immutable` only if asset names get content hashes).

- [ ] **Step 9: Run tests**

Run: `pytest tests/test_base_path.py -q && pytest -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add app/web/templating.py app/web/common.py app/main.py app/web/templates tests/test_base_path.py
git commit -m "feat(hub): sub-path URLs, /health, Waffle embed, HTML no-cache"
```

---

## Phase 4 — Container/Contract-Packaging

### Task 12: Dockerfile — Labels, .env.example, /health

**Files:**
- Modify: `Dockerfile`
- Modify: `.dockerignore` (ensure `.env.example` is NOT excluded — confirmed it isn't; do not add it)

- [ ] **Step 1: Add labels + copy .env.example**

Insert before `EXPOSE 8000`:

```dockerfile
COPY .env.example /app/.env.example

LABEL org.opencontainers.image.title="TimeHub" \
      org.opencontainers.image.description="Zentrale Zeiterfassung – Erfassung, Import, Export, Reporting" \
      org.opencontainers.image.vendor="mindsquare AG" \
      org.opencontainers.image.source="https://mindcode.mindsquare.de/<owner>/timehub" \
      org.opencontainers.image.version="2.0.0" \
      de.mindsquare.agenthub.category="productivity"
```

Replace `<owner>` with the real mindcode owner once known (coordination item). Update the healthcheck to hit `/health`:

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1
```

- [ ] **Step 2: Verify the image exposes the .env.example and health**

Run:
```bash
docker build -t timehub:hub-test .
docker run --rm timehub:hub-test cat /app/.env.example | head
docker run --rm -d -p 8000:8000 -e APP_ENV=dev --name th-test timehub:hub-test
sleep 6 && curl -fsS http://localhost:8000/health && docker rm -f th-test
```
Expected: `.env.example` prints; `/health` returns `{"status":"ok"}`.

- [ ] **Step 3: Commit**

```bash
git add Dockerfile
git commit -m "feat(docker): add Hub OCI labels, copy .env.example, /health healthcheck"
```

---

### Task 13: .env.example für den Hub

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Rewrite for the Hub runtime**

```dotenv
# --- App ---
APP_NAME=TimeHub
APP_ENV=production
LOG_LEVEL=info

# --- Security (session-cookie signing — set as Secret in the Hub) ---
# python -c "import secrets; print(secrets.token_urlsafe(48))"
SECRET_KEY=

# --- Agent Hub identity ---
# hub = trust X-MSQ-* headers (production). dev-bypass only outside production.
AUTH_MODE=hub
# Comma-separated emails that become TimeHub admins on first Hub login.
ADMIN_EMAILS=rick@mindsquare.de
# Mount path the Hub serves this app under (the slug, e.g. /timehub).
BASE_PATH=/timehub

# --- Database (SQLite in the appdata-timehub-data volume) ---
# Leave DATABASE_URL empty to use the default /app/data/timehub.sqlite.
# DATABASE_URL=

# --- Salesforce (app-managed; service user + per-user OAuth stay) ---
# Service-user fallback credentials (Fernet-encrypted at rest once saved).
# SF_LOGIN_URL=https://login.salesforce.com

# --- AI-assisted CSV mapping (optional, via mindsquare LiteLLM gateway) ---
# OPENAI_BASE_URL=http://litellm:4000/llm-admin/v1
# OPENAI_API_KEY=            # Virtual Key from the Hub admin (Secret)
# AI_MAPPING_MODEL=claude-sonnet

# --- MCP server (mounted at /mcp; authenticated via Hub mcp-bearer / X-MSQ) ---
# The Hub must have the app on proxy_settings.auth_mode=mcp-bearer.
MCP_ENABLED=true
```

- [ ] **Step 2: Verify it loads**

Run: `python -c "from app.config import Settings; print(Settings(_env_file='.env.example').app_name)"`
Expected: prints `TimeHub` (no validation error; SECRET_KEY empty is fine for non-production parse).

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "feat(config): Hub-ready .env.example (identity, SQLite, MCP off)"
```

---

### Task 14: README Container-Sektion + Navigation-Override-Hinweis

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add the Container block near the top**

```markdown
## Container

- 📦 **Package**: https://mindcode.mindsquare.de/<owner>/timehub/packages
- Image-Ref zum Einhängen in den Agent Hub:

  ```
  mindcode.mindsquare.de/<owner>/timehub:latest
  ```

Für eine pinned Version statt `:latest` einen Release-Tag verwenden (z. B. `:v2.0.0`).
```

- [ ] **Step 2: Document the Hub integration + (no) Navigation-Override**

Add a short "Agent Hub" section noting: identity via `X-MSQ-*`, Waffle embedded (so **no** Navigation-Override needed), SQLite in `appdata-timehub-data`, self-service backup/restore in the admin UI, and the MCP endpoint at `/timehub/mcp` (Hub `auth_mode=mcp-bearer`).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: add Hub container package link and integration notes"
```

---

### Task 15: MCP-Auth auf Hub-Identität (mcp-bearer) umstellen

**Files:**
- Modify: `app/mcp_server.py` (replace `ApiKeyAuthMiddleware` with X-MSQ identity; drop the API-key/bearer resolver)
- Test: `tests/test_mcp.py` (authenticate via dev-bypass / X-MSQ headers instead of an API key)

**Approach:** The Hub runs the app as `auth_mode=mcp-bearer`: it performs the M365/Entra OAuth, strips the inbound `Authorization`, and forwards `X-MSQ-*`. So the MCP server stays auth-free and reads the same identity as the rest of the app. The auth wrapper **must stay pure-ASGI** (BaseHTTPMiddleware would buffer the SSE stream). Transport is already Streamable HTTP (`stateless_http=True, json_response=True`) — no WebSocket, contract-compliant.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp.py — replace the auth setup
# In tests, APP_ENV=test → dev-bypass, so MCP requests resolve to the dev user
# without an API key. Assert an unauthenticated transport still mounts and a
# tool call works under the injected identity.
def test_mcp_uses_hub_identity(client):
    # A bare initialize call must be accepted (identity injected via dev-bypass),
    # not rejected with 401 for a missing API key.
    r = client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code != 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp.py::test_mcp_uses_hub_identity -v`
Expected: FAIL (current `ApiKeyAuthMiddleware` returns 401 without an API key).

- [ ] **Step 3: Replace the MCP auth middleware**

In `app/mcp_server.py`, delete `_resolve_auth` and `ApiKeyAuthMiddleware`, and replace with a Hub-identity wrapper (keep `_send_401`, `_user_id_var`, `_scope_var`):

```python
from app.config import get_settings
from app.identity import _dev_principal, principal_from_headers, resolve_user


class _ScopeHeaders:
    """Minimal .get() over the raw ASGI header list for principal_from_headers."""

    def __init__(self, raw):
        self._h = {k.decode().lower(): v.decode("latin-1") for k, v in raw}

    def get(self, key, default=None):
        return self._h.get(key.lower(), default)


class HubIdentityAuthMiddleware:
    """Pure-ASGI guard: resolve identity from X-MSQ-* (or dev-bypass), set the
    user contextvar, then delegate to the MCP app. The Hub (mcp-bearer) has
    already done the OAuth and stripped Authorization, so we never see a token."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if get_settings().resolved_auth_mode == "dev-bypass":
            principal = _dev_principal()
        else:
            principal = principal_from_headers(_ScopeHeaders(scope.get("headers", [])))
        if principal is None:
            await _send_401(send)
            return
        db = SessionLocal()
        try:
            user = resolve_user(db, principal)
            user_id = user.id if user.is_active else None
        finally:
            db.close()
        if user_id is None:
            await _send_401(send)
            return
        user_token = _user_id_var.set(user_id)
        scope_token = _scope_var.set("read_write")
        try:
            await self.app(scope, receive, send)
        finally:
            _user_id_var.reset(user_token)
            _scope_var.reset(scope_token)
```

Update `build_asgi_app` to wrap with `HubIdentityAuthMiddleware(mcp.streamable_http_app())`. Remove the now-unused imports (`ApiKey`, `hash_api_key`, `decode_token`, `_key_is_expired`) if nothing else in the module uses them (verify with grep).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mcp.py -v`
Expected: PASS (tools run under the dev identity). Update any test that asserted read-only-key scope behaviour: behind the Hub all users are `read_write`; cover scope-specific logic in `tests/test_api_key_scopes.py` directly instead.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server.py tests/test_mcp.py
git commit -m "feat(mcp): authenticate via Hub X-MSQ identity (mcp-bearer) instead of API key"
```

---

## Phase 5 — Repo + CI nach mindcode.mindsquare.de

### Task 16: Forgejo-Actions Build+Push

**Files:**
- Create: `.forgejo/workflows/build.yml`
- Modify/Remove: `.github/workflows/build.yml`, `deploy.yml` (deploy no longer needed — Hub pulls), keep `test.yml` logic ported to Forgejo

- [ ] **Step 1: Write the Forgejo build workflow**

```yaml
# .forgejo/workflows/build.yml
name: build
on:
  push:
    branches: [main]
    tags: ["v*"]

jobs:
  test:
    runs-on: docker
    container:
      image: python:3.12-slim
    steps:
      - uses: actions/checkout@v4
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: pytest -q

  image:
    needs: test
    runs-on: docker
    steps:
      - uses: actions/checkout@v4
      - name: Log in to mindcode registry
        uses: docker/login-action@v3
        with:
          registry: mindcode.mindsquare.de
          username: ${{ secrets.MINDCODE_USER }}
          password: ${{ secrets.MINDCODE_TOKEN }}
      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: |
            mindcode.mindsquare.de/${{ github.repository }}:latest
            mindcode.mindsquare.de/${{ github.repository }}:${{ github.sha }}
```

(Adjust runner labels/secret names to the mindcode Forgejo Actions setup — coordination item with the Forgejo admin.)

- [ ] **Step 2: Retire the GitHub workflows**

Delete `.github/workflows/build.yml` and `.github/workflows/deploy.yml`. Keep `.github/` only if a GitHub mirror remains; otherwise remove the directory.

- [ ] **Step 3: Verify workflow YAML parses**

Run: `python -c "import yaml,glob; [yaml.safe_load(open(f)) for f in glob.glob('.forgejo/workflows/*.yml')]; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add .forgejo/workflows/build.yml
git rm -r .github/workflows
git commit -m "ci: build and push image to mindcode via Forgejo Actions"
```

---

### Task 17: Repo-Remote nach mindcode umziehen (manuell, dokumentiert)

**Files:** none (runbook step)

- [ ] **Step 1: Document + execute the remote move**

```bash
# Create the repo on mindcode.mindsquare.de first (UI), then:
git remote rename origin github-old        # keep a reference, optional
git remote add origin https://mindcode.mindsquare.de/<owner>/timehub.git
git push -u origin main
git push origin --tags
```

Confirm CI triggers on mindcode and the package appears under the repo's Packages tab.

- [ ] **Step 2: Commit** (none — this is an ops step; note completion in the PR description.)

---

## Phase 6 — (Optional) LLM über mindsquare-Gateway

### Task 18: Anthropic-Call auf LiteLLM-Gateway umstellen

> Only do this if AI CSV-mapping stays enabled in the Hub. Contract A.2.9: no direct provider keys — route via the org LiteLLM gateway with a Virtual Key. Otherwise leave AI disabled (empty key) and skip.

**Files:**
- Modify: `app/services/ai_mapping.py`, `app/config.py`
- Test: `tests/test_ai_mapping.py` (existing — adapt to base_url/key from config)

- [ ] **Step 1: Add gateway settings**

In `app/config.py`:

```python
    openai_base_url: str | None = None   # LiteLLM gateway, e.g. http://litellm:4000/llm-admin/v1
    openai_api_key: str | None = None    # Virtual Key (Secret)
```

- [ ] **Step 2: Route the client through them**

In `app/services/ai_mapping.py`, build the Anthropic/OpenAI-compatible client from `openai_base_url`/`openai_api_key` (no hardcoded model; `ai_mapping_model` stays config-driven, default a current alias like `claude-sonnet`). Do not send fixed `temperature`/`max_tokens`.

- [ ] **Step 3: Run + commit**

Run: `pytest tests/test_ai_mapping.py -v` → PASS.

```bash
git add app/services/ai_mapping.py app/config.py tests/test_ai_mapping.py
git commit -m "feat(ai): route LLM calls via mindsquare LiteLLM gateway config"
```

---

## Phase 7 — Verify against contract + Onboarding

### Task 19: DoD-Check + Smoke-Test (gated by verification-before-completion)

- [ ] **Step 1: Run the full suite + build**

```bash
pytest -q
docker build -t timehub:hub .
```
Expected: all tests pass; image builds.

- [ ] **Step 2: Managed DoD (contract A.5) — tick each with evidence**

- [ ] `EXPOSE 8000` present (Dockerfile) — Task 2.
- [ ] Image pushed to mindcode — Task 16/17.
- [ ] README: package link + image-ref — Task 14.
- [ ] Waffle embedded — Task 11.
- [ ] `org.opencontainers.image.source` → mindcode repo — Task 12.
- [ ] OCI core labels + `de.mindsquare.agenthub.category` — Task 12.
- [ ] Startup diagnosable (effective config logged, fail-loud) — verify `app/config.py` validators + add a boot log line of `resolved_auth_mode`, `database_url` (redacted), `base_path`.
- [ ] `.env.example` in image — Task 12 (`docker run --rm timehub:hub cat /app/.env.example`).
- [ ] `GET /health` → 200 no auth — Task 11.
- [ ] Own login removed, identity from `X-MSQ-*` — Tasks 7–9.
- [ ] Local dev mode documented (`AUTH_MODE=dev-bypass`) — README.
- [ ] Persistent state in named volume; migrations forward-only/idempotent — Tasks 2/6.
- [ ] Self-service backup/restore works; initial data imported via restore — Task 4.
- [ ] HTML `Cache-Control: no-cache, must-revalidate` (pure-ASGI, MCP stream intact) — Task 11.
- [ ] MCP at `/mcp` authenticates via X-MSQ (mcp-bearer), no API key — Task 15; Hub set to `auth_mode=mcp-bearer`.
- [ ] Onboarding form filled (Task 20).
- [ ] Salesforce: unchanged app-managed — note as "not using Hub SF capability" with one-line rationale (no SF app permissions yet).

- [ ] **Step 3: Smoke test after the Hub admin deploys (contract A.4)**

```bash
curl -I https://aiforge.msr2.de/<slug>/                 # 401 or Hub login
docker ps --filter "label=msq.app=<slug>"               # container Up
# health from the hub container, browser end-to-end, HTML cache header present
```

---

### Task 20: Step F — Registration Summary für den Hub-Admin

- [ ] **Step 1: Produce the final block (no placeholders)** once owner/slug are fixed:

```
Hub-Registrierung für TimeHub
═══════════════════════════════════════════
Anzeigename:    TimeHub
Slug:           timehub
Beschreibung:   Zentrale Zeiterfassung – Erfassung, Import, Export, Reporting
Icon:           ⏱️
Kategorie:      productivity

Image-Ref:      mindcode.mindsquare.de/<owner>/timehub:latest
Container-Port: 8000
Health-Pfad:    /health

Zugriff:        alle angemeldeten Benutzer
Sichtbarkeit:   public

Env-Variablen:
  APP_ENV=production
  AUTH_MODE=hub
  ADMIN_EMAILS=rick@mindsquare.de
  BASE_PATH=/timehub
  MCP_ENABLED=true
Secret-Werte:
  SECRET_KEY=<48-byte random>

Volumes:
  appdata-timehub-data=/app/data
  appdata-timehub-uploads=/app/uploads

Timeout:        30s
Body-Limit:     höher setzen falls CSV/Datei-Uploads groß werden (Default 50 MB ok)
MCP / auth_mode: mcp-bearer aktivieren (Hub übernimmt M365-OAuth für /timehub/mcp)
Salesforce-Integration: nein (app-managed Service-User/OAuth bleibt intern)
```

- [ ] **Step 2: Hand off** — Git-Host-Credential für mindcode prüfen (Hub-Pull), `auth_mode=mcp-bearer` setzen lassen. **Kein** Volume-Seed nötig: den Initial-Datenimport macht der TimeHub-Admin selbst über **Datensicherung → Wiederherstellen** (Task 4).

---

## Self-Review

- **Spec coverage:** DB→SQLite (T1–T4), identity (T5–T10), sub-path/contract endpoints (T11), packaging (T12–T15), repo/CI move (T16–T17), optional LLM (T18), verify+onboard (T19–T20). Salesforce explicitly unchanged — covered by "do nothing + document rationale" (T19). Reserved `/auth/*` removed/renamed (T9). Waffle, logout-delegation, health, labels, .env.example all mapped.
- **Type/name consistency:** `HubPrincipal(subject,email,name,roles,guest)`, `resolve_user(db, principal)`, `principal_from_headers(headers)`, `resolve_request_user(request, db)`, `request.state.hub_user_id`, `join_base(root_path, path)`, `backup_svc.make_backup_zip`/`restore_from_zip`/`sqlite_path`, `HubIdentityAuthMiddleware` (MCP, pure-ASGI), settings `resolved_auth_mode`/`admin_email_set`/`normalized_base_path` — used consistently across tasks.
- **Known follow-ups (not in scope):** adopt the Hub Salesforce capability once SF app permissions exist (then per-user OAuth moves from app-managed to `X-MSQ-SF-*`); tighten `/static` to content-hashed `immutable` assets if a build step is added; optional Litestream for continuous off-box SQLite backup in addition to the GUI ZIP.
