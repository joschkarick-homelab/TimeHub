def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_login_and_create_entry(client):
    # login as initial admin
    r = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "testpass"},
    )
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}

    # create a project
    r = client.post(
        "/api/v1/projects",
        json={"name": "Demo", "code": "DEMO", "default_sync_target": "jira"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    # create a time entry
    r = client.post(
        "/api/v1/time-entries",
        json={
            "project_id": pid,
            "entry_date": "2026-05-27",
            "duration_minutes": 90,
            "description": "Initial work",
            "tags": ["alpha"],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text

    # markdown report
    r = client.get("/api/v1/reports/timesheet?format=markdown", headers=h)
    assert r.status_code == 200
    assert "Datum" in r.text and "Summe" in r.text

    # csv report
    r = client.get("/api/v1/reports/timesheet?format=csv", headers=h)
    assert r.status_code == 200
    assert "DEMO" in r.text


def test_api_key_flow(client):
    r = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "testpass"},
    )
    token = r.json()["access_token"]
    r = client.post(
        "/api/v1/auth/api-keys",
        json={"name": "ci-key"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    key = r.json()["key"]
    assert key.startswith("thk_")

    # use the api key
    r = client.get("/api/v1/auth/me", headers={"X-API-Key": key})
    assert r.status_code == 200
