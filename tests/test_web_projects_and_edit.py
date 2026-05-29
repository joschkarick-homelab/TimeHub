"""Web UI: projects CRUD, format editing, and import-error visibility."""

import io
from datetime import date


def _login_session(client) -> None:
    r = client.post(
        "/login",
        data={"email": "admin@example.com", "password": "testpass"},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text


def _login_api(client) -> str:
    r = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "testpass"},
    )
    return r.json()["access_token"]


def test_projects_page_renders_and_creates(client):
    _login_session(client)
    r = client.get("/projects")
    assert r.status_code == 200
    assert "Projekte" in r.text
    assert "Neues Projekt" in r.text  # admin sees the form

    r = client.post(
        "/projects",
        data={"name": "Acme Test", "code": "WEBCRUD", "customer": "Acme",
              "color": "#ff0000", "default_sync_target": "intern", "status": "active"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "/projects?flash=" in r.headers["location"]

    r = client.get("/projects")
    assert "WEBCRUD" in r.text
    assert "Acme Test" in r.text


def test_projects_edit_and_delete(client):
    _login_session(client)
    # ensure one to edit
    client.post(
        "/projects",
        data={"name": "ToEdit", "code": "WEBEDIT", "default_sync_target": "intern",
              "status": "active", "color": "#000000"},
        follow_redirects=False,
    )
    token = _login_api(client)
    h = {"Authorization": f"Bearer {token}"}
    pid = next(p["id"] for p in client.get("/api/v1/projects", headers=h).json() if p["code"] == "WEBEDIT")

    r = client.get(f"/projects/{pid}/edit")
    assert r.status_code == 200
    assert "ToEdit" in r.text

    r = client.post(
        f"/projects/{pid}/edit",
        data={"name": "Edited", "code": "WEBEDIT", "customer": "Acme",
              "color": "#00ff00", "default_sync_target": "jira", "status": "inactive"},
        follow_redirects=False,
    )
    assert r.status_code == 302

    after = client.get(f"/api/v1/projects/{pid}", headers=h).json()
    assert after["name"] == "Edited"
    assert after["default_sync_target"] == "jira"
    assert after["status"] == "inactive"

    # delete works (no entries linked to this project)
    r = client.post(f"/projects/{pid}/delete", follow_redirects=False)
    assert r.status_code == 302
    assert client.get(f"/api/v1/projects/{pid}", headers=h).status_code == 404


def test_projects_create_rejects_duplicate_code(client):
    _login_session(client)
    client.post(
        "/projects",
        data={"name": "First", "code": "DUPECODE", "default_sync_target": "intern",
              "status": "active", "color": "#111111"},
        follow_redirects=False,
    )
    r = client.post(
        "/projects",
        data={"name": "Second", "code": "DUPECODE", "default_sync_target": "intern",
              "status": "active", "color": "#222222"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=" in r.headers["location"]


def test_import_errors_are_visible_in_response(client):
    """The whole point of this fix: a partial-failure import must show the
    per-row reasons in the rendered page, not just in a hidden summary."""
    _login_session(client)
    token = _login_api(client)
    h = {"Authorization": f"Bearer {token}"}

    fmt_id = client.post(
        "/api/v1/import-formats",
        json={
            "name": "ErrFmt",
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
    ).json()["id"]

    # Three rows: 1 OK, 2 will fail (bad date, bad duration)
    csv_body = (
        "Datum;Stunden;Projekt;Notiz\n"
        "2026-05-25;1,5;ERR-OK;goes through\n"
        "not-a-date;1,5;ERR-OK;invalid date\n"
        "2026-05-26;not-a-number;ERR-OK;invalid duration\n"
    )
    files = {"file": ("data.csv", io.BytesIO(csv_body.encode("utf-8")))}
    r = client.post(
        "/import",
        data={"format_id": str(fmt_id)},
        files=files,
        follow_redirects=False,
    )
    # IMPORTANT: no redirect — the result page renders inline.
    assert r.status_code == 200, r.text
    body = r.text
    assert "Fehlgeschlagene Zeilen" in body
    # both failed rows are listed with their specific error
    assert "invalid date" in body or "not-a-date" in body
    assert "not-a-number" in body
    # success counter is right too
    assert "1 importiert" in body or "1 Einträge" in body


def test_format_edit_persists_changes(client):
    _login_session(client)
    token = _login_api(client)
    h = {"Authorization": f"Bearer {token}"}

    fmt_id = client.post(
        "/api/v1/import-formats",
        json={
            "name": "EditMe",
            "separator": ",",
            "date_format": "%Y-%m-%d",
            "column_map": {"entry_date": "Date", "duration_hours": "Hrs"},
        },
        headers=h,
    ).json()["id"]

    r = client.get(f"/import-formats/{fmt_id}/edit")
    assert r.status_code == 200
    assert "EditMe" in r.text
    assert "Date" in r.text and "Hrs" in r.text

    # Change name + tweak the mapping (target-keyed: {target: source})
    new_map = '{"entry_date": "Date", "duration_minutes": "Hours", "project_code": "Project"}'
    r = client.post(
        f"/import-formats/{fmt_id}/edit",
        data={
            "name": "EditedFmt",
            "source_hint": "custom",
            "separator": ";",
            "encoding": "utf-8",
            "date_format": "%d.%m.%Y",
            "time_format": "%H:%M",
            "default_project_code": "",
            "notes": "edited",
            "column_map_json": new_map,
        },
        follow_redirects=False,
    )
    assert r.status_code == 302

    after = client.get(f"/api/v1/import-formats/{fmt_id}", headers=h).json()
    assert after["name"] == "EditedFmt"
    assert after["separator"] == ";"
    assert after["date_format"] == "%d.%m.%Y"
    assert after["column_map"] == {
        "entry_date": "Date",
        "duration_minutes": "Hours",
        "project_code": "Project",
    }
