"""TimeEntry CRUD, theme switcher, profile (Salesforce IDs), and the
dropdown-deduplication fix for projects where code == name."""



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


def _make_project(client, code: str, name: str | None = None) -> int:
    h = {"Authorization": f"Bearer {_login_api(client)}"}
    r = client.post(
        "/api/v1/projects",
        json={"name": name or code, "code": code, "default_sync_target": "intern"},
        headers=h,
    )
    if r.status_code == 201:
        return r.json()["id"]
    r = client.get("/api/v1/projects", headers=h)
    return next(p["id"] for p in r.json() if p["code"] == code)


def _make_entry(client, project_id: int, day: str = "2026-05-25") -> int:
    h = {"Authorization": f"Bearer {_login_api(client)}"}
    r = client.post(
        "/api/v1/time-entries",
        json={"project_id": project_id, "entry_date": day,
              "duration_minutes": 60, "description": "for-crud"},
        headers=h,
    )
    return r.json()["id"]


# ---------- TimeEntry CRUD ----------

def test_entry_edit_form_renders_and_persists(client):
    _login_session(client)
    pid = _make_project(client, "ECRUD")
    eid = _make_entry(client, pid)

    r = client.get(f"/entries/{eid}/edit")
    assert r.status_code == 200
    assert "Eintrag bearbeiten" in r.text
    assert "for-crud" in r.text  # current description prefilled

    new_day = "2026-05-30"
    r = client.post(
        f"/entries/{eid}/edit",
        data={"entry_date": new_day, "project_id": str(pid),
              "duration_minutes": "120", "description": "edited"},
        follow_redirects=False,
    )
    assert r.status_code == 302

    token = _login_api(client)
    after = client.get(f"/api/v1/time-entries/{eid}",
                       headers={"Authorization": f"Bearer {token}"}).json()
    assert after["duration_minutes"] == 120
    assert after["description"] == "edited"
    assert after["entry_date"] == new_day


def test_entry_delete_removes_row(client):
    _login_session(client)
    pid = _make_project(client, "EDEL")
    eid = _make_entry(client, pid)

    r = client.post(f"/entries/{eid}/delete", follow_redirects=False)
    assert r.status_code == 302

    token = _login_api(client)
    r = client.get(f"/api/v1/time-entries/{eid}",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404


def test_entry_edit_rejects_other_users_entry(client):
    """A non-admin can't edit someone else's entry."""
    admin_token = _login_api(client)
    h_admin = {"Authorization": f"Bearer {admin_token}"}
    client.post(
        "/api/v1/users",
        json={"email": "other@example.com", "password": "secret123",
              "full_name": "Other", "is_admin": False},
        headers=h_admin,
    )

    # admin makes an entry against own user, then other user tries to edit
    pid = _make_project(client, "EOWN")
    eid = _make_entry(client, pid)

    # log in as other via session
    r = client.post(
        "/login", data={"email": "other@example.com", "password": "secret123"},
        follow_redirects=False,
    )
    assert r.status_code == 302

    r = client.get(f"/entries/{eid}/edit")
    assert r.status_code == 403


# ---------- Dropdown dedup ----------

def test_project_display_label_omits_code():
    """Project.display_label shows the name (with optional customer) and
    deliberately omits the internal project code; it falls back to the code
    only when no name is set."""
    from app.models import Project

    p1 = Project(name="Test", code="Test")
    assert p1.display_label == "Test"

    p2 = Project(name="Acme Onboarding", code="ACME-1")
    assert p2.display_label == "Acme Onboarding"

    p3 = Project(name="Acme Onboarding", code="ACME-1", customer="Acme")
    assert p3.display_label == "Acme Onboarding (Acme)"

    p4 = Project(name="", code="ONLY-CODE")
    assert p4.display_label == "ONLY-CODE"


def test_dashboard_dropdown_uses_display_label(client):
    """Render the dashboard and confirm the project select doesn't double up."""
    _login_session(client)
    _make_project(client, "DROPDUP")  # name == code on purpose
    r = client.get("/")
    assert r.status_code == 200
    # The duplicated form would render "DROPDUP – DROPDUP"; we now want a single one.
    assert "DROPDUP – DROPDUP" not in r.text
    assert "DROPDUP" in r.text


# ---------- Theme switcher ----------

def test_theme_switcher_sets_cookie_and_renders_data_attr(client):
    _login_session(client)
    r = client.get("/")
    # default is dark
    assert 'data-theme="dark"' in r.text

    r = client.post(
        "/settings/theme", data={"theme": "light"}, follow_redirects=False,
        headers={"referer": "/"},
    )
    assert r.status_code == 302
    cookie = r.headers["set-cookie"]
    assert "theme=light" in cookie

    r = client.get("/", cookies={"theme": "light"})
    assert 'data-theme="light"' in r.text


def test_theme_switcher_falls_back_on_bogus_value(client):
    _login_session(client)
    r = client.post(
        "/settings/theme", data={"theme": "neon-pink"}, follow_redirects=False,
        headers={"referer": "/"},
    )
    assert r.status_code == 302
    cookie = r.headers["set-cookie"]
    assert "theme=dark" in cookie  # forced back to the default theme


# ---------- Profile ----------

def test_profile_page_saves_name_and_no_longer_renders_salesforce(client):
    _login_session(client)
    r = client.get("/profile")
    assert r.status_code == 200
    assert "Mein Profil" in r.text
    # The Salesforce section was removed from the profile — the API user is
    # now admin-managed (siehe /users), die Resource kommt aus der Assignment.
    assert "Salesforce-Anbindung" not in r.text
    assert 'name="salesforce_user_id"' not in r.text
    assert 'name="salesforce_contact_id"' not in r.text

    r = client.post("/profile", data={"full_name": "Admin Full"},
                    follow_redirects=False)
    assert r.status_code == 302
    token = _login_api(client)
    me = client.get("/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {token}"}).json()
    assert me["full_name"] == "Admin Full"
