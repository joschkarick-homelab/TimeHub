"""Running-timer lifecycle (start → current → stop/cancel) and the weekly
hours aggregation endpoint. Auth uses an API key, mirroring how the Raycast
extension and the MCP server will call these endpoints."""

from datetime import date, datetime, timedelta


def _api_key(client) -> dict:
    token = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "testpass"},
    ).json()["access_token"]
    key = client.post(
        "/api/v1/auth/api-keys",
        json={"name": "timer-test"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()["key"]
    return {"X-API-Key": key}


def _project(client, h, code="TMR") -> int:
    r = client.post(
        "/api/v1/projects",
        json={"name": "Timer Proj", "code": code, "default_sync_target": "intern"},
        headers=h,
    )
    if r.status_code == 201:
        return r.json()["id"]
    return next(
        p["id"] for p in client.get("/api/v1/projects", headers=h).json() if p["code"] == code
    )


def test_no_timer_returns_null(client):
    h = _api_key(client)
    r = client.get("/api/v1/timer/current", headers=h)
    assert r.status_code == 200
    assert r.json() is None


def test_start_current_and_stop(client):
    h = _api_key(client)
    pid = _project(client, h)

    r = client.post(
        "/api/v1/timer/start",
        json={"project_id": pid, "description": "deep work", "tags": ["billable"]},
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["project_id"] == pid
    assert body["project_code"] == "TMR"
    assert body["description"] == "deep work"
    assert body["elapsed_seconds"] >= 0

    # current reflects the running timer
    cur = client.get("/api/v1/timer/current", headers=h).json()
    assert cur["id"] == body["id"]

    # a second start is rejected while one runs
    assert client.post("/api/v1/timer/start", json={"project_id": pid}, headers=h).status_code == 409

    # stop materializes an entry and clears the timer
    stopped = client.post("/api/v1/timer/stop", headers=h)
    assert stopped.status_code == 201, stopped.text
    entry = stopped.json()
    assert entry["project_id"] == pid
    assert entry["description"] == "deep work"
    assert entry["tags"] == ["billable"]
    assert entry["duration_minutes"] >= 1

    assert client.get("/api/v1/timer/current", headers=h).json() is None


def test_start_by_code_and_backdate_gives_duration(client):
    h = _api_key(client)
    _project(client, h, code="BACK")
    started = (datetime.now() - timedelta(minutes=42)).isoformat()

    r = client.post(
        "/api/v1/timer/start",
        json={"project_code": "BACK", "started_at": started},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["elapsed_seconds"] >= 42 * 60 - 5

    entry = client.post("/api/v1/timer/stop", json={"round_to_minutes": 15}, headers=h).json()
    # 42 min rounds up to the next 15-min step → 45
    assert entry["duration_minutes"] == 45


def test_stop_without_running_timer_404(client):
    h = _api_key(client)
    assert client.post("/api/v1/timer/stop", headers=h).status_code == 404


def test_cancel_discards_timer(client):
    h = _api_key(client)
    pid = _project(client, h, code="CANC")
    assert client.post("/api/v1/timer/start", json={"project_id": pid}, headers=h).status_code == 201
    assert client.delete("/api/v1/timer/current", headers=h).status_code == 204
    assert client.get("/api/v1/timer/current", headers=h).json() is None
    assert client.delete("/api/v1/timer/current", headers=h).status_code == 404


def test_start_unknown_project_400(client):
    h = _api_key(client)
    assert client.post("/api/v1/timer/start", json={"project_code": "NOPE"}, headers=h).status_code == 400


def test_weekly_hours(client):
    h = _api_key(client)
    pid = _project(client, h, code="WEEK")
    today = date.today()
    monday = today - timedelta(days=today.weekday())

    for d, mins in ((monday, 60), (monday + timedelta(days=1), 90)):
        client.post(
            "/api/v1/time-entries",
            json={
                "project_id": pid,
                "entry_date": d.isoformat(),
                "duration_minutes": mins,
                "description": "wk",
            },
            headers=h,
        )

    r = client.get("/api/v1/reports/weekly", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["date_from"] == monday.isoformat()
    assert body["date_to"] == (monday + timedelta(days=6)).isoformat()
    # At least our 150 minutes for this project this week.
    proj = next(p for p in body["by_project"] if p["project_id"] == pid)
    assert proj["minutes"] >= 150
    assert body["total_minutes"] >= 150
    assert any(t["target"] == "intern" for t in body["by_target"])


def test_weekly_hours_empty_past_week(client):
    h = _api_key(client)
    r = client.get("/api/v1/reports/weekly?week_offset=-520", headers=h)
    assert r.status_code == 200
    assert r.json()["total_minutes"] == 0
