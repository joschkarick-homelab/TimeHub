"""Projects are per-user: scoped lists, owner-only access, per-user code
uniqueness, per-user CSV auto-create — plus the dashboard week default."""

from datetime import date, timedelta


def _api_token(client, email, pw):
    return email


def _h(email):
    from tests.conftest import hub_headers

    return hub_headers(email)


def _make_user(client, admin_h, email):
    client.post(
        "/api/v1/users",
        json={"email": email, "password": "secret123", "full_name": "U", "is_admin": False},
        headers=admin_h,
    )
    return _api_token(client, email, "secret123")


def _new_project(client, h, code):
    return client.post(
        "/api/v1/projects",
        json={"name": code.title(), "code": code, "default_sync_target": "intern"},
        headers=h,
    )


def test_same_code_allowed_for_different_users(client):
    admin = _h(_api_token(client, "admin@example.com", "testpass"))
    other = _h(_make_user(client, admin, "pp-other@example.com"))

    assert _new_project(client, admin, "DUPCODE").status_code == 201
    # Same code, different owner → allowed (per-user uniqueness).
    assert _new_project(client, other, "DUPCODE").status_code == 201
    # Same owner, same code again → conflict.
    assert _new_project(client, admin, "DUPCODE").status_code == 409


def test_project_lists_are_scoped_per_user(client):
    admin = _h(_api_token(client, "admin@example.com", "testpass"))
    other = _h(_make_user(client, admin, "pp-scope@example.com"))
    _new_project(client, admin, "ADMINONLY")
    _new_project(client, other, "OTHERONLY")

    admin_codes = {p["code"] for p in client.get("/api/v1/projects", headers=admin).json()}
    other_codes = {p["code"] for p in client.get("/api/v1/projects", headers=other).json()}
    assert "ADMINONLY" in admin_codes and "OTHERONLY" not in admin_codes
    assert "OTHERONLY" in other_codes and "ADMINONLY" not in other_codes


def test_cannot_access_or_mutate_other_users_project(client):
    admin = _h(_api_token(client, "admin@example.com", "testpass"))
    other = _h(_make_user(client, admin, "pp-foreign@example.com"))
    pid = _new_project(client, admin, "PRIVATE").json()["id"]

    assert client.get(f"/api/v1/projects/{pid}", headers=other).status_code == 404
    assert client.patch(f"/api/v1/projects/{pid}", json={"name": "x"}, headers=other).status_code == 404
    assert client.delete(f"/api/v1/projects/{pid}", headers=other).status_code == 404
    # owner still has access
    assert client.get(f"/api/v1/projects/{pid}", headers=admin).status_code == 200


def test_entry_cannot_reference_foreign_project(client):
    admin = _h(_api_token(client, "admin@example.com", "testpass"))
    other = _h(_make_user(client, admin, "pp-entry@example.com"))
    pid = _new_project(client, admin, "ADMINPRJ").json()["id"]
    r = client.post(
        "/api/v1/time-entries",
        json={"project_id": pid, "entry_date": "2026-05-10", "duration_minutes": 60},
        headers=other,
    )
    assert r.status_code == 400


def test_csv_import_creates_project_scoped_to_user(client):
    admin_h = _h(_api_token(client, "admin@example.com", "testpass"))
    _make_user(client, admin_h, "pp-imp@example.com")

    from app.db import SessionLocal
    from app.models import Project, User
    from app.services.csv_import import import_csv

    csv = b"date,code,mins\n2026-05-10,SHAREDIMP,60\n"
    cmap = {"entry_date": "date", "project_code": "code", "duration_minutes": "mins"}
    with SessionLocal() as db:
        a = db.query(User).filter_by(email="admin@example.com").one().id
        b = db.query(User).filter_by(email="pp-imp@example.com").one().id
        import_csv(db, a, csv, column_map=cmap, separator=",", date_format="%Y-%m-%d")
        import_csv(db, b, csv, column_map=cmap, separator=",", date_format="%Y-%m-%d")
        owners = sorted(p.user_id for p in db.query(Project).filter(Project.code == "SHAREDIMP").all())
    assert owners == sorted([a, b])


def test_non_admin_can_manage_own_projects_via_web(client):
    admin = _h(_api_token(client, "admin@example.com", "testpass"))
    _make_user(client, admin, "pp-web@example.com")
    from tests.conftest import act_as
    act_as(client, "pp-web@example.com")
    # The create form is no longer admin-only.
    assert "Neues Projekt" in client.get("/projects").text
    r = client.post(
        "/projects",
        data={"name": "My Web Project", "default_sync_target": "intern"},
        follow_redirects=False,
    )
    assert r.status_code == 302 and "flash=" in (r.headers.get("location") or "")
    assert "My Web Project" in client.get("/projects").text


def test_dashboard_default_window_is_current_week(client):
    from tests.conftest import act_as
    act_as(client, "admin@example.com")
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    page = client.get("/")
    assert f'value="{monday.isoformat()}"' in page.text
    assert f'value="{sunday.isoformat()}"' in page.text
