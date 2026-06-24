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


def test_de_date_filter_formats_german_with_optional_weekday():
    from app.web.common import de_date

    d = date(2026, 6, 17)  # a Wednesday
    assert de_date(d) == "17.06.2026"
    assert de_date(d, weekday=True) == "Mi, 17.06.2026"
    # tolerant of None / empty / ISO strings handed in by templates
    assert de_date(None) == ""
    assert de_date("") == ""
    assert de_date("2026-06-17") == "17.06.2026"


def test_de_day_label_relative_and_weekday():
    from app.web.common import de_day_label

    today = date(2026, 6, 18)  # a Thursday
    assert de_day_label(date(2026, 6, 18), today=today) == "Heute · 18.06.2026"
    assert de_day_label(date(2026, 6, 17), today=today) == "Gestern · 17.06.2026"
    # older days fall back to the weekday prefix
    assert de_day_label(date(2026, 6, 15), today=today) == "Mo · 15.06.2026"
    assert de_day_label(None, today=today) == ""


def test_dashboard_renders_dates_in_german_format(client):
    """Grouped entries must show the German DD.MM.YYYY date, not ISO."""
    _login_session(client)
    api_token = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "testpass"},
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {api_token}"}
    proj = client.post(
        "/api/v1/projects",
        json={"name": "DE", "code": "DEFMT", "default_sync_target": "intern"},
        headers=h,
    )
    pid = proj.json().get("id")
    if pid is None:
        pid = next(p["id"] for p in client.get("/api/v1/projects", headers=h).json()
                   if p["code"] == "DEFMT")
    client.post(
        "/api/v1/time-entries",
        json={"project_id": pid, "entry_date": "2026-06-17",
              "duration_minutes": 90, "description": "de fmt"},
        headers=h,
    )
    r = client.get("/?date_from=2026-06-17&date_to=2026-06-17")
    assert r.status_code == 200
    # German date rendered once per group (prefix is today-relative, so we only
    # assert on the stable date part, not Heute/Gestern/weekday).
    assert "17.06.2026" in r.text


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
