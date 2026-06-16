"""Dashboard UI: default-to-today, filtering, and daily subtotals."""

from datetime import date


def _login_session(client) -> None:
    r = client.post(
        "/login",
        data={"email": "admin@example.com", "password": "testpass"},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text


def test_dashboard_renders_today_as_default_and_groups_by_day(client):
    _login_session(client)
    r = client.get("/")
    assert r.status_code == 200
    today = date.today().isoformat()
    # The "Datum" input on the entry form must default to today.
    assert f'value="{today}"' in r.text
    # The header changed from "Letzte Einträge" to "Einträge (gruppiert nach Tag)".
    assert "gruppiert nach Tag" in r.text


def test_dashboard_filters_by_date_range(client):
    _login_session(client)
    r = client.get("/?date_from=2099-01-01&date_to=2099-01-31")
    assert r.status_code == 200
    # Far-future window has nothing — fallback message shows.
    assert "Keine Einträge im gewählten Zeitraum" in r.text


def test_export_via_import_format_endpoint(client):
    """End-to-end: create entries, create a format, export via the web endpoint,
    confirm we get a downloadable CSV in the format's shape."""
    _login_session(client)
    # need an entry in scope — use the JSON API
    api_token = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "testpass"},
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {api_token}"}

    # ensure a project + entry exist
    proj = client.post(
        "/api/v1/projects",
        json={"name": "Exp", "code": "EXP", "default_sync_target": "intern"},
        headers=h,
    )
    pid = proj.json().get("id")
    if pid is None:
        pid = next(p["id"] for p in client.get("/api/v1/projects", headers=h).json() if p["code"] == "EXP")
    today = date.today().isoformat()
    client.post(
        "/api/v1/time-entries",
        json={"project_id": pid, "entry_date": today, "duration_minutes": 120, "description": "exp"},
        headers=h,
    )
    fmt = client.post(
        "/api/v1/import-formats",
        json={
            "name": "ExportFmt",
            "separator": ",",
            "date_format": "%Y-%m-%d",
            "column_map": {
                "entry_date": "When",
                "duration_hours": "Hours",
                "project_code": "Project",
                "description": "Note",
            },
        },
        headers=h,
    ).json()

    r = client.get(
        f"/entries/export?format_id={fmt['id']}&date_from={today}&date_to={today}&project_id={pid}"
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment;" in r.headers["content-disposition"]
    body = r.text
    # Header line uses the source-side headers from the format
    assert body.splitlines()[0] == "When,Hours,Project,Note"
    # At least one data row mentions today's date and the project code
    assert today in body
    assert "EXP" in body
