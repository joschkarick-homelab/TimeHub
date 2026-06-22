"""Dashboard CRUD niceties + import preview:

- delete / bulk-delete keep the active filter (redirect back to the same view)
- mass-select bulk delete removes several entries at once
- the import flow previews (dry-run) before anything is written, then confirms
"""

import base64
import io
from datetime import date


def _login_session(client) -> None:
    r = client.post(
        "/login",
        data={"email": "admin@example.com", "password": "testpass"},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text


def _api_token(client) -> str:
    r = client.post("/api/v1/auth/login",
                    json={"email": "admin@example.com", "password": "testpass"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _ensure_project(client, h, code="CRUDP") -> int:
    r = client.post("/api/v1/projects",
                    json={"name": code, "code": code, "default_sync_target": "intern"},
                    headers=h)
    if r.status_code == 201:
        return r.json()["id"]
    return next(p["id"] for p in client.get("/api/v1/projects", headers=h).json()
                if p["code"] == code)


def _make_entry(client, h, pid, day, minutes=60) -> int:
    r = client.post("/api/v1/time-entries",
                    json={"project_id": pid, "entry_date": day,
                          "duration_minutes": minutes, "description": "x"},
                    headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------- filter preservation ----------

def test_delete_preserves_filter_via_next(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_api_token(client)}"}
    pid = _ensure_project(client, h)
    eid = _make_entry(client, h, pid, "2026-03-15")

    nxt = f"/?date_from=2026-03-01&date_to=2026-03-31&project_id={pid}"
    r = client.post(f"/entries/{eid}/delete", data={"next": nxt}, follow_redirects=False)
    assert r.status_code == 302
    # We bounce back to the exact filtered view, not to "/".
    assert r.headers["location"] == nxt


def test_delete_without_next_falls_back_to_root(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_api_token(client)}"}
    pid = _ensure_project(client, h)
    eid = _make_entry(client, h, pid, "2026-03-16")
    r = client.post(f"/entries/{eid}/delete", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/"


def test_dashboard_renders_next_on_delete_forms(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_api_token(client)}"}
    pid = _ensure_project(client, h)
    _make_entry(client, h, pid, date.today().isoformat())
    r = client.get(f"/?project_id={pid}")
    assert r.status_code == 200
    # The hidden next field carries the current filter into the delete form.
    assert 'name="next"' in r.text
    assert f"project_id={pid}" in r.text
    # Mass-select affordances are present.
    assert "Mehrfachauswahl" in r.text
    assert '/entries/bulk-delete' in r.text


# ---------- edit modal + filter-preserving actions ----------

def test_edit_modal_returns_bare_fragment(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_api_token(client)}"}
    pid = _ensure_project(client, h)
    eid = _make_entry(client, h, pid, "2026-04-10")

    nxt = "/?date_from=2026-04-01&date_to=2026-04-30"
    r = client.get(f"/entries/{eid}/edit?modal=1&next={nxt}")
    assert r.status_code == 200
    # The fragment is just the form — no full-page chrome (<html>/<head>).
    assert "<html" not in r.text
    assert 'class="th-entry-form' in r.text
    # Return target is carried into the form (as the next hidden field) so
    # saving lands back on the filter. (& is HTML-escaped in the attribute.)
    assert 'name="next"' in r.text
    assert "date_from=2026-04-01" in r.text


def test_edit_form_still_renders_full_page_without_modal(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_api_token(client)}"}
    pid = _ensure_project(client, h)
    eid = _make_entry(client, h, pid, "2026-04-11")
    r = client.get(f"/entries/{eid}/edit")
    assert r.status_code == 200
    assert "<html" in r.text
    assert "Eintrag bearbeiten" in r.text


def test_dashboard_edit_buttons_open_modal(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_api_token(client)}"}
    pid = _ensure_project(client, h)
    _make_entry(client, h, pid, date.today().isoformat())
    r = client.get("/")
    assert r.status_code == 200
    # Edit is now a modal trigger, not a navigation to the standalone page.
    assert "data-edit-url=" in r.text
    assert "modal=1" in r.text


def test_create_entry_preserves_filter_via_next(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_api_token(client)}"}
    pid = _ensure_project(client, h)
    nxt = f"/?date_from=2026-05-01&date_to=2026-05-31&project_id={pid}"
    r = client.post(
        "/entries",
        data={"entry_date": "2026-05-15", "project_id": str(pid),
              "duration_minutes": "60", "next": nxt},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == nxt


def test_mark_synced_preserves_filter_via_next(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_api_token(client)}"}
    pid = _ensure_project(client, h)
    eid = _make_entry(client, h, pid, "2026-06-15")
    nxt = f"/?date_from=2026-06-01&date_to=2026-06-30&project_id={pid}"
    r = client.post("/entries/mark-synced",
                    data={"entry_ids": [eid], "next": nxt},
                    follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(nxt)
    assert "flash=1" in loc


# ---------- bulk delete (mass-select) ----------

def test_bulk_delete_removes_selected_and_keeps_filter(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_api_token(client)}"}
    pid = _ensure_project(client, h)
    a = _make_entry(client, h, pid, "2026-09-01")
    b = _make_entry(client, h, pid, "2026-09-02")
    c = _make_entry(client, h, pid, "2026-09-03")

    nxt = f"/?date_from=2026-09-01&date_to=2026-09-30&project_id={pid}"
    r = client.post("/entries/bulk-delete",
                    data={"entry_ids": [a, b], "next": nxt},
                    follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(nxt)
    assert "flash=2" in loc

    remaining = {e["id"] for e in client.get("/api/v1/time-entries", headers=h).json()}
    assert a not in remaining
    assert b not in remaining
    assert c in remaining


def test_bulk_delete_empty_selection_is_safe(client):
    _login_session(client)
    r = client.post("/entries/bulk-delete", data={"next": "/?project_id=1"},
                    follow_redirects=False)
    assert r.status_code == 302
    assert "error=Keine" in r.headers["location"]


# ---------- import preview (dry-run) ----------

def _preview_format(client, h) -> int:
    return client.post("/api/v1/import-formats", json={
        "name": "PreviewFmt",
        "separator": ";",
        "date_format": "%Y-%m-%d",
        "column_map": {
            "entry_date": "Datum",
            "duration_hours": "Stunden",
            "project_code": "Projekt",
            "description": "Notiz",
        },
    }, headers=h).json()["id"]


def test_import_preview_does_not_persist_then_confirm_imports(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_api_token(client)}"}
    fmt_id = _preview_format(client, h)

    csv_body = (
        "Datum;Stunden;Projekt;Notiz\n"
        "2026-07-01;1,5;PREVIEWPROJ;erste Zeile\n"
        "2026-07-02;2,0;PREVIEWPROJ;zweite Zeile\n"
    )

    def count_proj():
        return sum(1 for p in client.get("/api/v1/projects", headers=h).json()
                   if p["code"] == "PREVIEWPROJ")

    assert count_proj() == 0

    files = {"file": ("data.csv", io.BytesIO(csv_body.encode("utf-8")))}
    r = client.post("/import/preview", data={"format_id": fmt_id}, files=files)
    assert r.status_code == 200, r.text
    assert "Vorschau" in r.text
    assert "erste Zeile" in r.text
    assert "Jetzt importieren" in r.text
    # Dry-run must not have created the project or any entries.
    assert count_proj() == 0

    # Confirm via the carried-over base64 payload (as the template does).
    raw_b64 = base64.b64encode(csv_body.encode("utf-8")).decode("ascii")
    r2 = client.post("/import", data={"format_id": fmt_id, "raw_b64": raw_b64})
    assert r2.status_code == 200, r2.text
    assert "2 Einträge importiert" in r2.text
    assert count_proj() == 1


def test_import_preview_reports_bad_rows_without_writing(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_api_token(client)}"}
    fmt_id = _preview_format(client, h)
    # Second row has a non-parseable duration → would be skipped.
    csv_body = (
        "Datum;Stunden;Projekt;Notiz\n"
        "2026-08-01;1,0;PREVBAD;ok\n"
        "2026-08-02;abc;PREVBAD;kaputt\n"
    )
    files = {"file": ("data.csv", io.BytesIO(csv_body.encode("utf-8")))}
    r = client.post("/import/preview", data={"format_id": fmt_id}, files=files)
    assert r.status_code == 200, r.text
    assert "übersprungen" in r.text
    # nothing written yet
    assert not any(p["code"] == "PREVBAD"
                   for p in client.get("/api/v1/projects", headers=h).json())


def test_import_confirm_without_payload_errors(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_api_token(client)}"}
    fmt_id = _preview_format(client, h)
    r = client.post("/import", data={"format_id": fmt_id})
    assert r.status_code == 400
    assert "Keine Datei" in r.text
