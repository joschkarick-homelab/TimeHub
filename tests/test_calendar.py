"""Calendar view: page rendering, drag-create endpoint, move/resize endpoint."""


def _login_session(client) -> None:
    r = client.post(
        "/login",
        data={"email": "admin@example.com", "password": "testpass"},
        follow_redirects=False,
    )
    assert r.status_code == 302


def _token(client) -> str:
    return client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "testpass"},
    ).json()["access_token"]


def _project(client, code: str) -> int:
    h = {"Authorization": f"Bearer {_token(client)}"}
    r = client.post(
        "/api/v1/projects",
        json={"name": code.title(), "code": code, "default_sync_target": "intern"},
        headers=h,
    )
    if r.status_code in (200, 201):
        return r.json()["id"]
    return next(
        p["id"] for p in client.get("/api/v1/projects", headers=h).json() if p["code"] == code
    )


# ---------- page ----------

def test_calendar_page_renders_week(client):
    _login_session(client)
    r = client.get("/calendar")
    assert r.status_code == 200
    assert 'id="cal-scroll"' in r.text
    # week view shows 7 day columns worth of weekday markers
    assert "Heute" in r.text


def test_calendar_page_single_day(client):
    _login_session(client)
    r = client.get("/calendar?days=1&start=2026-05-27")
    assert r.status_code == 200
    assert "27.05." in r.text


def test_calendar_clamps_days(client):
    _login_session(client)
    r = client.get("/calendar?days=99")
    assert r.status_code == 200  # clamped to 7, no crash


def test_calendar_requires_login(client):
    r = client.get("/calendar", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


# ---------- drag-create ----------

def test_calendar_create_entry(client):
    _login_session(client)
    pid = _project(client, "CALCREATE")
    r = client.post(
        "/calendar/entries",
        json={"project_id": pid, "entry_date": "2026-05-27",
              "start_time": "09:00", "end_time": "10:30", "description": "drag created"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["duration_minutes"] == 90
    assert body["start"] == 540 and body["end"] == 630

    # appears as a positioned block on that day
    page = client.get("/calendar?days=1&start=2026-05-27")
    assert "drag created" in page.text
    assert "cal-block" in page.text


def test_calendar_create_rejects_end_before_start(client):
    _login_session(client)
    pid = _project(client, "CALBAD")
    r = client.post(
        "/calendar/entries",
        json={"project_id": pid, "entry_date": "2026-05-27",
              "start_time": "12:00", "end_time": "11:00"},
    )
    assert r.status_code == 400
    assert "error" in r.json()


def test_calendar_create_requires_login(client):
    r = client.post(
        "/calendar/entries",
        json={"project_id": 1, "entry_date": "2026-05-27",
              "start_time": "09:00", "end_time": "10:00"},
    )
    assert r.status_code == 401


# ---------- move / resize ----------

def test_calendar_move_updates_time_and_day(client):
    _login_session(client)
    pid = _project(client, "CALMOVE")
    created = client.post(
        "/calendar/entries",
        json={"project_id": pid, "entry_date": "2026-05-27",
              "start_time": "09:00", "end_time": "10:00", "description": "movable"},
    ).json()
    eid = created["id"]

    r = client.post(
        f"/calendar/entries/{eid}/move",
        json={"entry_date": "2026-05-28", "start_time": "14:00", "end_time": "15:30"},
    )
    assert r.status_code == 200
    moved = r.json()
    assert moved["start"] == 840 and moved["end"] == 930
    assert moved["duration_minutes"] == 90

    h = {"Authorization": f"Bearer {_token(client)}"}
    entry = client.get(f"/api/v1/time-entries/{eid}", headers=h).json()
    assert entry["entry_date"] == "2026-05-28"
    assert entry["start_time"] == "14:00:00"


def test_calendar_move_missing_entry(client):
    _login_session(client)
    r = client.post(
        "/calendar/entries/999999/move",
        json={"entry_date": "2026-05-28", "start_time": "14:00", "end_time": "15:00"},
    )
    assert r.status_code == 404


# ---------- untimed entries ----------

def test_calendar_shows_untimed_entry(client):
    _login_session(client)
    pid = _project(client, "CALUNTIMED")
    # duration-only entry (no start/end) lands in the "Ohne Uhrzeit" strip
    client.post(
        "/entries",
        data={"entry_date": "2026-05-29", "project_id": str(pid),
              "duration_minutes": "60", "description": "no clock"},
        follow_redirects=False,
    )
    page = client.get("/calendar?days=1&start=2026-05-29")
    assert page.status_code == 200
    assert "Ohne Uhrzeit" in page.text
