def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_login_and_create_entry(client):
    # admin identity comes from the Hub (X-MSQ-* on the client fixture)
    from tests.conftest import hub_headers
    h = hub_headers("admin@example.com")

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


def test_api_key_flow(client, raw_client):
    # Mint a key while acting as admin via the Hub identity.
    r = client.post("/api/v1/auth/api-keys", json={"name": "ci-key"})
    assert r.status_code == 201, r.text
    key = r.json()["key"]
    assert key.startswith("thk_")

    # Use the api key — on raw_client so the KEY (not a Hub identity) authenticates.
    r = raw_client.get("/api/v1/auth/me", headers={"X-API-Key": key})
    assert r.status_code == 200
