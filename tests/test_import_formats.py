import io
from unittest.mock import patch

from app.schemas.import_format import ImportFormatSuggestion


def _login(client, email="admin@example.com", password="testpass") -> str:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_import_format_crud_and_visibility(client):
    admin = _login(client)
    h_admin = {"Authorization": f"Bearer {admin}"}

    # admin creates a second user via the API
    r = client.post(
        "/api/v1/users",
        json={"email": "consultant@example.com", "password": "secret123", "full_name": "Con", "is_admin": False},
        headers=h_admin,
    )
    assert r.status_code == 201, r.text

    # consultant logs in
    consultant = _login(client, "consultant@example.com", "secret123")
    h_user = {"Authorization": f"Bearer {consultant}"}

    # consultant creates a private format (is_global ignored for non-admins)
    r = client.post(
        "/api/v1/import-formats",
        json={
            "name": "Mein Toggl",
            "source_hint": "toggl",
            "separator": ",",
            "date_format": "%Y-%m-%d",
            "column_map": {"entry_date": "Start date", "description": "Description"},
            "is_global": True,
        },
        headers=h_user,
    )
    assert r.status_code == 201, r.text
    fmt_id = r.json()["id"]
    assert r.json()["is_global"] is False  # forced to False for non-admin

    # admin cannot see consultant's private format via scope=mine, but admin
    # explicitly listing scope=all does (admin override)
    r = client.get("/api/v1/import-formats?scope=visible", headers=h_admin)
    ids_admin_visible = [f["id"] for f in r.json()]
    assert fmt_id not in ids_admin_visible

    r = client.get("/api/v1/import-formats?scope=all", headers=h_admin)
    assert fmt_id in [f["id"] for f in r.json()]

    # admin promotes the format to global
    r = client.patch(
        f"/api/v1/import-formats/{fmt_id}",
        json={"is_global": True},
        headers=h_admin,
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_global"] is True

    # now everyone sees it
    r = client.get("/api/v1/import-formats?scope=visible", headers=h_admin)
    assert fmt_id in [f["id"] for f in r.json()]

    # consultant deletes their own format
    r = client.delete(f"/api/v1/import-formats/{fmt_id}", headers=h_user)
    assert r.status_code == 204


def test_suggest_endpoint_uses_ai_service(client, monkeypatch):
    token = _login(client)
    h = {"Authorization": f"Bearer {token}"}

    fake_suggestion = ImportFormatSuggestion(
        source_hint="toggl",
        separator=",",
        encoding="utf-8",
        date_format="%Y-%m-%d",
        time_format="%H:%M:%S",
        column_map={"entry_date": "Start date", "description": "Description"},
        notes="Erkannt: Toggl-Export.",
        detected_headers=["Start date", "Description", "Duration"],
    )

    with patch("app.api.import_formats.suggest_mapping", return_value=fake_suggestion):
        files = {"file": ("toggl.csv", io.BytesIO(b"Start date,Description,Duration\n2026-05-27,Demo,01:30:00\n"))}
        r = client.post("/api/v1/import-formats/suggest", files=files, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["source_hint"] == "toggl"
        assert r.json()["column_map"]["description"] == "Description"


def test_run_imports_via_saved_format(client):
    token = _login(client)
    h = {"Authorization": f"Bearer {token}"}

    # ensure a project exists
    r = client.post(
        "/api/v1/projects",
        json={"name": "Imp", "code": "IMP", "default_sync_target": "intern"},
        headers=h,
    )
    pid = r.json().get("id")
    if pid is None:
        # already created in previous test; fetch by listing
        r = client.get("/api/v1/projects", headers=h)
        pid = next(p["id"] for p in r.json() if p["code"] == "IMP")

    r = client.post(
        "/api/v1/import-formats",
        json={
            "name": "Custom",
            "separator": ";",
            "date_format": "%d.%m.%Y",
            "column_map": {
                "entry_date": "Datum",
                "duration_hours": "Stunden",
                "project_code": "Projekt",
                "description": "Notiz",
            },
        },
        headers=h,
    )
    fmt_id = r.json()["id"]

    csv_body = "Datum;Stunden;Projekt;Notiz\n27.05.2026;1,5;IMP;via import-format\n"
    files = {"file": ("data.csv", io.BytesIO(csv_body.encode("utf-8")))}
    r = client.post(f"/api/v1/import-formats/{fmt_id}/run", files=files, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] == 1
    assert body["failed"] == 0
