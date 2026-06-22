"""Data isolation: time-entry data is scoped to the requesting user — admins
included. Only dedicated admin-management views (user CRUD) stay cross-user."""

import json


def _api_token(client, email, pw):
    return client.post("/api/v1/auth/login",
                       json={"email": email, "password": pw}).json()["access_token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def _make_user(client, admin_h, email):
    client.post("/api/v1/users",
                json={"email": email, "password": "secret123", "full_name": "Other U",
                      "is_admin": False},
                headers=admin_h)
    return _api_token(client, email, "secret123")


def _project(client, h, code):
    r = client.post("/api/v1/projects",
                    json={"name": code.title(), "code": code, "default_sync_target": "intern"},
                    headers=h)
    if r.status_code == 201:
        return r.json()["id"]
    # Already created by an earlier test (session-scoped DB) — reuse it.
    return next(p["id"] for p in client.get("/api/v1/projects", headers=h).json()
                if p["code"] == code)


def _entry(client, h, pid, day="2026-05-22", minutes=60, desc="iso"):
    return client.post("/api/v1/time-entries",
                       json={"project_id": pid, "entry_date": day,
                             "duration_minutes": minutes, "description": desc},
                       headers=h).json()


def _setup_other_with_entry(client):
    admin = _h(_api_token(client, "admin@example.com", "testpass"))
    other_email = "iso-other@example.com"
    other = _h(_make_user(client, admin, other_email))
    pid = _project(client, other, "ISOOTHER")
    e = _entry(client, other, pid, desc="other-secret")
    return admin, other, other_email, e


# ---------- API: list ----------

def test_admin_list_excludes_other_users_entries(client):
    admin, other, other_email, e = _setup_other_with_entry(client)
    ids = {x["id"] for x in client.get("/api/v1/time-entries", headers=admin).json()}
    assert e["id"] not in ids
    # the owner still sees it
    own = {x["id"] for x in client.get("/api/v1/time-entries", headers=other).json()}
    assert e["id"] in own


def test_admin_list_ignores_user_id_query_param(client):
    admin, other, other_email, e = _setup_other_with_entry(client)
    # user_id is no longer an accepted filter — admin still only sees own data.
    other_uid = e["user_id"]
    ids = {x["id"] for x in
           client.get(f"/api/v1/time-entries?user_id={other_uid}", headers=admin).json()}
    assert e["id"] not in ids


# ---------- API: get / patch / delete ----------

def test_admin_cannot_access_other_users_entry(client):
    admin, other, other_email, e = _setup_other_with_entry(client)
    eid = e["id"]
    assert client.get(f"/api/v1/time-entries/{eid}", headers=admin).status_code == 404
    assert client.patch(f"/api/v1/time-entries/{eid}",
                        json={"description": "x"}, headers=admin).status_code == 404
    assert client.delete(f"/api/v1/time-entries/{eid}", headers=admin).status_code == 404
    # still there for the owner
    assert client.get(f"/api/v1/time-entries/{eid}", headers=other).status_code == 200


# ---------- API: create on behalf of others ----------

def test_admin_cannot_create_entry_for_other_user(client):
    admin, other, other_email, e = _setup_other_with_entry(client)
    other_uid = e["user_id"]
    # admin owns this project; force a foreign user_id → must be rejected
    pid = _project(client, admin, "ISOADMIN")
    r = client.post("/api/v1/time-entries",
                    json={"project_id": pid, "entry_date": "2026-05-23",
                          "duration_minutes": 30, "user_id": other_uid},
                    headers=admin)
    assert r.status_code == 403


# ---------- API: reports ----------

def test_admin_reports_api_only_returns_own_data(client):
    admin, other, other_email, e = _setup_other_with_entry(client)
    body = client.get("/api/v1/reports/timesheet?format=json", headers=admin).text
    data = json.loads(body)
    emails = {row["user_email"] for row in data}
    assert other_email not in emails


# ---------- Web: reports + edit/delete ----------

def _login(client, email, pw):
    r = client.post("/login", data={"email": email, "password": pw}, follow_redirects=False)
    assert r.status_code == 302


def test_web_reports_admin_sees_only_own_and_no_employee_filter(client):
    admin, other, other_email, e = _setup_other_with_entry(client)
    _login(client, "admin@example.com", "testpass")
    page = client.get("/reports?preset=weekly_detailed&date_from=2026-05-01&date_to=2026-05-31")
    assert page.status_code == 200
    # The cross-user "Mitarbeiter" filter is gone.
    assert 'name="user_id"' not in page.text
    assert "other-secret" not in page.text


def test_web_admin_cannot_edit_or_delete_other_users_entry(client):
    admin, other, other_email, e = _setup_other_with_entry(client)
    _login(client, "admin@example.com", "testpass")
    eid = e["id"]
    assert client.get(f"/entries/{eid}/edit", follow_redirects=False).status_code == 403
    assert client.post(f"/entries/{eid}/delete", follow_redirects=False).status_code == 403
