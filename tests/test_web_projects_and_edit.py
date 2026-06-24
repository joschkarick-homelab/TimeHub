"""Web UI: projects CRUD, format editing, and import-error visibility."""

import io


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
    # The internal project code is deliberately hidden from the UI; the
    # human-facing name and customer are what's shown.
    assert "Acme Test" in r.text
    assert "Acme" in r.text


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


def test_customer_autocomplete_offers_existing_customers(client):
    _login_session(client)
    # Create projects with distinct customers so they become suggestions.
    for name, cust in [("AutoA", "Globex GmbH"), ("AutoB", "Acme AG")]:
        client.post(
            "/projects",
            data={"name": name, "customer": cust, "default_sync_target": "intern",
                  "status": "active", "color": "#123456"},
            follow_redirects=False,
        )

    # Create form (projects page) wires up the autocomplete and ships the list.
    r = client.get("/projects")
    assert r.status_code == 200
    assert "data-customer-ac" in r.text
    assert "window.TH_CUSTOMERS" in r.text
    assert "Globex GmbH" in r.text
    assert "Acme AG" in r.text

    # Edit form carries the same suggestions.
    token = _login_api(client)
    h = {"Authorization": f"Bearer {token}"}
    pid = next(p["id"] for p in client.get("/api/v1/projects", headers=h).json()
               if p["name"] == "AutoA")
    r = client.get(f"/projects/{pid}/edit")
    assert r.status_code == 200
    assert "data-customer-ac" in r.text
    assert "Acme AG" in r.text


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


# ── Import aus Salesforce-Projekten ──────────────────────────────────────────

_SF_IMPORT_FAKE = [
    {"assignment_id": "a01000000000111", "name": "Asklepios Faktura",
     "customer": "Asklepios", "number": "P00042",
     "label": "Asklepios Faktura (Asklepios · P00042)"},
    {"assignment_id": "a01000000000222", "name": "Globex Rollout",
     "customer": "Globex", "number": "P00043",
     "label": "Globex Rollout (Globex · P00043)"},
]


class _FakeSFClient:
    """Stand-in client. The dropdown's live SOQL (via _sync_dynamic_options)
    hits .query on the projects page, so it must not blow up; the import list
    itself is stubbed via assignments_for_import below."""

    def query(self, soql):
        return {"records": []}


def _patch_sf_import(monkeypatch):
    from app.services import salesforce as sfs
    monkeypatch.setattr(sfs, "credentials_configured", lambda db: True)
    monkeypatch.setattr(sfs, "client_from_settings", lambda db: _FakeSFClient())
    monkeypatch.setattr(
        sfs, "assignments_for_import",
        lambda client, email: [dict(a) for a in _SF_IMPORT_FAKE],
    )


def test_sf_import_button_and_guard_when_not_configured(client, monkeypatch):
    from app.services import salesforce as sfs
    _login_session(client)
    monkeypatch.setattr(sfs, "credentials_configured", lambda db: False)
    # No Salesforce → no button on the projects page …
    assert "Import aus Salesforce-Projekten" not in client.get("/projects").text
    # … and the import route bounces back with an error.
    r = client.get("/projects/import-salesforce", follow_redirects=False)
    assert r.status_code == 302
    assert "error=Salesforce" in r.headers["location"]


def test_sf_import_lists_creates_and_dedupes(client, monkeypatch):
    _login_session(client)
    _patch_sf_import(monkeypatch)
    token = _login_api(client)
    h = {"Authorization": f"Bearer {token}"}

    # Button is offered once SF is configured.
    assert "Import aus Salesforce-Projekten" in client.get("/projects").text

    # Import page lists both open assignments with customer + number.
    r = client.get("/projects/import-salesforce")
    assert r.status_code == 200
    assert "Asklepios Faktura" in r.text and "Globex Rollout" in r.text
    assert "P00042" in r.text

    # Create only the first one.
    r = client.post(
        "/projects/import-salesforce",
        data={"assignment_ids": ["a01000000000111"]},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "flash=1+Projekt" in r.headers["location"]

    # The new project exists, is linked to Salesforce …
    projects = client.get("/api/v1/projects", headers=h).json()
    created = next(p for p in projects if p["code"] == "P00042")
    assert created["name"] == "Asklepios Faktura"
    assert created["default_sync_target"] == "salesforce"

    # … and is no longer offered for import, while the other one still is.
    r = client.get("/projects/import-salesforce")
    assert "Asklepios Faktura" not in r.text  # already imported → filtered out
    assert "Globex Rollout" in r.text


def test_sf_import_links_assignment_and_varies_colors(client, monkeypatch):
    from app.services import salesforce as sfs
    _login_session(client)
    # Own, unique assignments so the shared session DB from other tests can't
    # dedupe these away.
    monkeypatch.setattr(sfs, "credentials_configured", lambda db: True)
    monkeypatch.setattr(sfs, "client_from_settings", lambda db: _FakeSFClient())
    fakes = [
        {"assignment_id": "a01000000000333", "name": "Color One",
         "customer": "C1", "number": "P00050", "label": "x"},
        {"assignment_id": "a01000000000444", "name": "Color Two",
         "customer": "C2", "number": "P00051", "label": "x"},
    ]
    monkeypatch.setattr(sfs, "assignments_for_import",
                        lambda client, email: [dict(a) for a in fakes])
    token = _login_api(client)
    h = {"Authorization": f"Bearer {token}"}

    r = client.post(
        "/projects/import-salesforce",
        data={"assignment_ids": ["a01000000000333", "a01000000000444"]},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "flash=2+Projekt" in r.headers["location"]

    by_code = {p["code"]: p for p in client.get("/api/v1/projects", headers=h).json()}
    p1, p2 = by_code["P00050"], by_code["P00051"]
    # assignment_id is stored under the salesforce target …
    assert p1["sync_metadata"]["salesforce"]["assignment_id"] == "a01000000000333"
    assert p2["sync_metadata"]["salesforce"]["assignment_id"] == "a01000000000444"
    # … and the two imported projects got distinct colours.
    assert p1["color"] != p2["color"]


def test_sf_import_requires_selection(client, monkeypatch):
    _login_session(client)
    _patch_sf_import(monkeypatch)
    r = client.post("/projects/import-salesforce", data={}, follow_redirects=False)
    assert r.status_code == 302
    assert "error=Keine" in r.headers["location"]
