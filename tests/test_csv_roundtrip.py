"""Coverage for the new import/export niceties:

- import auto-creates unknown projects and matches existing ones loosely
- the same ImportFormat exports & re-imports cleanly (round-trip)
"""

import io


def _login(client, email="admin@example.com", password="testpass") -> str:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_import_auto_creates_unknown_projects_and_matches_loosely(client):
    token = _login(client)
    h = {"Authorization": f"Bearer {token}"}

    # Seed an existing project that should match loosely.
    r = client.post(
        "/api/v1/projects",
        json={"name": "ACME Onboarding", "code": "ACME-1", "default_sync_target": "intern"},
        headers=h,
    )
    assert r.status_code in (201, 400)  # 400 if a previous test created it

    fmt = client.post(
        "/api/v1/import-formats",
        json={
            "name": "RoundTrip",
            "separator": ";",
            "date_format": "%Y-%m-%d",
            "column_map": {
                "entry_date": "Datum",
                "duration_hours": "Stunden",
                "project_code": "Projekt",
                "description": "Notiz",
            },
        },
        headers=h,
    ).json()

    csv_body = (
        "Datum;Stunden;Projekt;Notiz\n"
        "2026-05-25;1,5;acme 1;loose match — should reuse ACME-1\n"
        "2026-05-26;2,0;NEUKUNDE;unknown — should be auto-created\n"
    )
    files = {"file": ("data.csv", io.BytesIO(csv_body.encode("utf-8")))}
    r = client.post(f"/api/v1/import-formats/{fmt['id']}/run", files=files, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] == 2
    assert body["failed"] == 0
    assert "NEUKUNDE" in body["created_projects"]
    # the loose match did NOT create a new project
    assert "acme 1" not in body["created_projects"]
    assert "ACME-1" not in body["created_projects"]


def test_format_round_trips_export_to_import(client):
    """Export the entries we just made using the same format, then re-import
    that export — should produce the same number of entries with no errors."""
    token = _login(client)
    h = {"Authorization": f"Bearer {token}"}

    # Use the format we created in the previous test (fetch by name).
    formats = client.get("/api/v1/import-formats", headers=h).json()
    fmt = next(f for f in formats if f["name"] == "RoundTrip")

    # Export the user's whole timesheet via the new web endpoint.
    # The web endpoint requires session auth, so we go via the JSON timesheet
    # to know what should be in the file, then exercise the export shape
    # by calling the service directly.
    from app.db import SessionLocal
    from app.models import ImportFormat, Project, TimeEntry, User
    from app.services.reports import export_via_import_format

    with SessionLocal() as db:
        admin = db.query(User).filter_by(email="admin@example.com").one()
        rows = (
            db.query(TimeEntry, Project, User)
            .join(Project, Project.id == TimeEntry.project_id)
            .join(User, User.id == TimeEntry.user_id)
            .filter(TimeEntry.user_id == admin.id)
            .all()
        )
        f = db.get(ImportFormat, fmt["id"])
        body, encoding = export_via_import_format(
            rows,
            f.column_map,
            separator=f.separator,
            date_format=f.date_format,
            time_format=f.time_format,
        )

    assert body.startswith("Datum;Stunden;Projekt;Notiz")
    line_count = body.count("\n") - 1  # subtract header
    assert line_count >= 2

    # Re-import the same file — no errors, same project codes recognized.
    files = {"file": ("export.csv", io.BytesIO(body.encode("utf-8")))}
    r = client.post(f"/api/v1/import-formats/{fmt['id']}/run", files=files, headers=h)
    assert r.status_code == 201, r.text
    body2 = r.json()
    assert body2["failed"] == 0
    assert body2["created"] == line_count
    # All projects were already known on the second pass.
    assert body2["created_projects"] == []
