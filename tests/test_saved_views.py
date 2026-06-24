"""Saved views (dashboard + reports), relative date ranges, and the new
dashboard customer filter."""

from datetime import date


def _login_session(client) -> None:
    r = client.post(
        "/login",
        data={"email": "admin@example.com", "password": "testpass"},
        follow_redirects=False,
    )
    assert r.status_code == 302


def _login_api(client) -> str:
    return client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "testpass"},
    ).json()["access_token"]


def _make_project(client, code: str, customer: str | None = None) -> int:
    h = {"Authorization": f"Bearer {_login_api(client)}"}
    body = {"name": code, "code": code, "default_sync_target": "intern"}
    if customer:
        body["customer"] = customer
    r = client.post("/api/v1/projects", json=body, headers=h)
    if r.status_code == 201:
        return r.json()["id"]
    r = client.get("/api/v1/projects", headers=h)
    return next(p["id"] for p in r.json() if p["code"] == code)


def _make_entry(client, project_id: int, day: str, desc: str) -> int:
    h = {"Authorization": f"Bearer {_login_api(client)}"}
    r = client.post(
        "/api/v1/time-entries",
        json={"project_id": project_id, "entry_date": day,
              "duration_minutes": 60, "description": desc},
        headers=h,
    )
    return r.json()["id"]


# ---------- relative date ranges ----------

def test_resolve_date_range_tokens():
    from app.web.common import resolve_date_range

    today = date(2026, 6, 17)  # a Wednesday
    assert resolve_date_range("all", None, None, today=today) == (None, None)
    assert resolve_date_range("this_week", None, None, today=today) == (
        date(2026, 6, 15), date(2026, 6, 21),
    )
    assert resolve_date_range("last_week", None, None, today=today) == (
        date(2026, 6, 8), date(2026, 6, 14),
    )
    assert resolve_date_range("this_month", None, None, today=today) == (
        date(2026, 6, 1), date(2026, 6, 30),
    )
    assert resolve_date_range("last_month", None, None, today=today) == (
        date(2026, 5, 1), date(2026, 5, 31),
    )
    assert resolve_date_range("this_year", None, None, today=today) == (
        date(2026, 1, 1), date(2026, 12, 31),
    )
    # custom / unknown pass the explicit dates straight through
    df, dt = date(2026, 2, 3), date(2026, 2, 9)
    assert resolve_date_range("custom", df, dt, today=today) == (df, dt)
    assert resolve_date_range("bogus", df, dt, today=today) == (df, dt)


# ---------- dashboard customer filter ----------

def test_dashboard_customer_filter(client):
    _login_session(client)
    acme = _make_project(client, "ACMECUST", customer="Acme")
    other = _make_project(client, "OTHRCUST", customer="Globex")
    _make_entry(client, acme, "2026-03-10", "acme-work")
    _make_entry(client, other, "2026-03-10", "globex-work")

    # Customer filter narrows to just Acme's entries.
    r = client.get("/?date_range=all&customer=Acme")
    assert r.status_code == 200
    assert "acme-work" in r.text
    assert "globex-work" not in r.text


# ---------- saved dashboard views ----------

def test_save_apply_and_delete_dashboard_view(client):
    _login_session(client)
    pid = _make_project(client, "DASHVIEW", customer="ViewCo")
    _make_entry(client, pid, "2026-04-01", "dash-view-entry")

    # Save the current filter as a named dashboard view.
    r = client.post(
        "/views",
        data={"name": "MyDash", "kind": "dashboard", "date_range": "all",
              "customer": "ViewCo", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "view=" in r.headers["location"]

    # It shows up as a chip on the dashboard.
    r = client.get("/")
    assert "MyDash" in r.text

    # Applying it (?view=id) reproduces the saved filter.
    from app.db import SessionLocal
    from app.models import SavedView
    with SessionLocal() as db:
        v = db.query(SavedView).filter_by(name="MyDash", kind="dashboard").one()
        vid = v.id
    r = client.get(f"/?view={vid}")
    assert r.status_code == 200
    assert "dash-view-entry" in r.text

    # Same name overwrites instead of duplicating.
    client.post(
        "/views",
        data={"name": "MyDash", "kind": "dashboard", "date_range": "this_year",
              "next": "/"},
        follow_redirects=False,
    )
    with SessionLocal() as db:
        rows = db.query(SavedView).filter_by(name="MyDash", kind="dashboard").all()
        assert len(rows) == 1
        assert rows[0].date_range == "this_year"

    # Delete it.
    r = client.post(f"/views/{vid}/delete", data={"next": "/"}, follow_redirects=False)
    assert r.status_code == 302
    r = client.get("/")
    assert "MyDash" not in r.text


# ---------- saved report views ----------

def test_save_and_apply_report_view(client):
    _login_session(client)
    pid = _make_project(client, "REPVIEW", customer="RepCo")
    _make_entry(client, pid, "2026-05-05", "rep-view-entry")

    r = client.post(
        "/views",
        data={"name": "MonatsReport", "kind": "reports",
              "date_range": "this_year",
              "group_by": ["month", "project"],
              "detailed": "1", "next": "/reports"},
        follow_redirects=False,
    )
    assert r.status_code == 302

    from app.db import SessionLocal
    from app.models import SavedView
    with SessionLocal() as db:
        v = db.query(SavedView).filter_by(name="MonatsReport", kind="reports").one()
        assert v.group_by == ["month", "project"]
        assert v.detailed is True
        vid = v.id

    r = client.get("/reports")
    assert "MonatsReport" in r.text

    r = client.get(f"/reports?view={vid}")
    assert r.status_code == 200
    # The report renders with the saved grouping active.
    assert "rep-view-entry" in r.text or "RepCo" in r.text


def test_views_are_per_user(client):
    """A view saved by one user is invisible to another."""
    admin_h = {"Authorization": f"Bearer {_login_api(client)}"}
    client.post(
        "/api/v1/users",
        json={"email": "viewer@example.com", "password": "secret123",
              "full_name": "Viewer", "is_admin": False},
        headers=admin_h,
    )
    _login_session(client)
    client.post(
        "/views",
        data={"name": "AdminOnly", "kind": "dashboard", "date_range": "all", "next": "/"},
        follow_redirects=False,
    )

    # Switch to the other user's session.
    r = client.post(
        "/login", data={"email": "viewer@example.com", "password": "secret123"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    r = client.get("/")
    assert "AdminOnly" not in r.text
