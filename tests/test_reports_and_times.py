"""Reporting page (presets + custom grouping + subtotals) and the
start/end-time entry path on the dashboard form."""

from datetime import date


def _login_session(client) -> None:
    from tests.conftest import act_as

    act_as(client, "admin@example.com")


def _login_api(client) -> str:
    from tests.conftest import act_as

    act_as(client, "admin@example.com")
    return "hub-identity"


# ---------- start/end time entry ----------

def test_create_entry_with_start_end_derives_duration(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_login_api(client)}"}
    pid = client.post(
        "/api/v1/projects",
        json={"name": "Timed", "code": "TIMED", "default_sync_target": "intern"},
        headers=h,
    )
    pid = pid.json().get("id") or next(
        p["id"] for p in client.get("/api/v1/projects", headers=h).json() if p["code"] == "TIMED"
    )

    r = client.post(
        "/entries",
        data={"entry_date": "2026-05-27", "project_id": str(pid),
              "start_time": "09:00", "end_time": "11:30", "description": "with times"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/"

    # newest entry should be 150 minutes, with start/end persisted
    entries = client.get("/api/v1/time-entries", headers=h).json()
    mine = [e for e in entries if e["description"] == "with times"][0]
    assert mine["duration_minutes"] == 150
    assert mine["start_time"] == "09:00:00"
    assert mine["end_time"] == "11:30:00"


def test_create_entry_end_before_start_is_rejected(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_login_api(client)}"}
    pid = next(p["id"] for p in client.get("/api/v1/projects", headers=h).json() if p["code"] == "TIMED")
    r = client.post(
        "/entries",
        data={"entry_date": "2026-05-27", "project_id": str(pid),
              "start_time": "12:00", "end_time": "10:00"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=" in r.headers["location"]


def test_create_entry_without_duration_or_times_is_rejected(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_login_api(client)}"}
    pid = next(p["id"] for p in client.get("/api/v1/projects", headers=h).json() if p["code"] == "TIMED")
    r = client.post(
        "/entries",
        data={"entry_date": "2026-05-27", "project_id": str(pid)},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=" in r.headers["location"]


def test_duration_field_still_works_without_times(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_login_api(client)}"}
    pid = next(p["id"] for p in client.get("/api/v1/projects", headers=h).json() if p["code"] == "TIMED")
    r = client.post(
        "/entries",
        data={"entry_date": "2026-05-28", "project_id": str(pid),
              "duration_minutes": "45", "description": "plain duration"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    entries = client.get("/api/v1/time-entries", headers=h).json()
    mine = [e for e in entries if e["description"] == "plain duration"][0]
    assert mine["duration_minutes"] == 45


# ---------- report builder service ----------

def test_report_builder_nests_and_subtotals():
    from types import SimpleNamespace

    from app.services.report_builder import build_report

    def row(day, code, name, mins, customer="Acme"):
        e = SimpleNamespace(entry_date=date.fromisoformat(day), id=1,
                            duration_minutes=mins, start_time=None, end_time=None,
                            description="")
        p = SimpleNamespace(code=code, name=name, customer=customer,
                            display_label=f"{code} – {name}")
        u = SimpleNamespace(full_name="Admin", email="a@x")
        return (e, p, u)

    rows = [
        row("2026-05-25", "A", "Alpha", 60),
        row("2026-05-25", "B", "Beta", 120),
        row("2026-05-26", "A", "Alpha", 30),
    ]
    rep = build_report(rows, ["day", "project"], detailed=False)
    assert rep["total_minutes"] == 210
    assert rep["count"] == 3
    # two day-groups
    assert len(rep["groups"]) == 2
    day1 = rep["groups"][0]
    assert day1["label"] == "2026-05-25"
    assert day1["total_minutes"] == 180
    # nested project groups under day 1
    assert {c["label"] for c in day1["children"]} == {"A – Alpha", "B – Beta"}


def test_report_builder_detailed_attaches_entries():
    from types import SimpleNamespace

    from app.services.report_builder import build_report

    e = SimpleNamespace(entry_date=date(2026, 5, 25), id=1, duration_minutes=90,
                        start_time=None, end_time=None, description="x")
    p = SimpleNamespace(code="A", name="Alpha", customer="Acme", display_label="A – Alpha")
    u = SimpleNamespace(full_name="Admin", email="a@x")
    rep = build_report([(e, p, u)], ["project"], detailed=True)
    leaf = rep["groups"][0]
    assert leaf["entries"] and leaf["entries"][0][0] is e


# ---------- reports page ----------

def test_reports_page_presets_render(client):
    _login_session(client)
    for preset in ("weekly_detailed", "weekly_day_project", "monthly_project",
                   "by_customer", "project_detailed"):
        r = client.get(f"/reports?preset={preset}")
        assert r.status_code == 200, preset
        assert "Gesamt" in r.text


def test_reports_custom_grouping(client):
    _login_session(client)
    r = client.get("/reports?group_by=customer&group_by=project&detailed=1")
    assert r.status_code == 200
    # the chosen dimensions appear as group labels in the result
    assert "Kunde" in r.text
    assert "Projekt" in r.text


def test_reports_default_view(client):
    _login_session(client)
    r = client.get("/reports")
    assert r.status_code == 200
    # default preset is weekly_detailed
    assert "Woche" in r.text or "Keine Einträge" in r.text
