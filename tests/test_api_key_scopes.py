"""Scoped + expiring API keys: read / tracking / read_write enforcement on the
REST API and the profile UI, plus expiry rejection.

The API key — not a Hub identity — must authenticate these requests, so the
key-bearing calls go through ``raw_client`` (no X-MSQ headers); otherwise
``get_current_user`` would resolve the Hub identity first and ignore the key.
Keys are minted while acting as the admin on the identity-carrying ``client``.
"""

import re
from datetime import UTC, datetime, timedelta


def _make_key(client, scope: str, expires_in_days: int | None = None) -> str:
    from tests.conftest import act_as

    act_as(client, "admin@example.com")
    payload: dict = {"name": f"{scope}-key", "scope": scope}
    if expires_in_days is not None:
        payload["expires_in_days"] = expires_in_days
    return client.post("/api/v1/auth/api-keys", json=payload).json()["key"]


def _project(raw_client, key: str, code: str) -> int:
    r = raw_client.post(
        "/api/v1/projects",
        json={"name": f"Scope {code}", "code": code, "default_sync_target": "intern"},
        headers={"X-API-Key": key},
    )
    if r.status_code == 201:
        return r.json()["id"]
    return next(
        p["id"] for p in raw_client.get("/api/v1/projects", headers={"X-API-Key": key}).json()
        if p["code"] == code
    )


def test_read_scope_allows_get_blocks_writes(client, raw_client):
    rw = _make_key(client, "read_write")
    pid = _project(raw_client, rw, "RSCOPE")
    read = _make_key(client, "read")

    # GET works…
    assert raw_client.get("/api/v1/time-entries", headers={"X-API-Key": read}).status_code == 200
    assert raw_client.get("/api/v1/projects", headers={"X-API-Key": read}).status_code == 200

    # …writes are rejected with 403.
    w1 = raw_client.post(
        "/api/v1/time-entries",
        json={"project_id": pid, "entry_date": "2026-06-01", "duration_minutes": 30},
        headers={"X-API-Key": read},
    )
    assert w1.status_code == 403
    assert raw_client.post(
        "/api/v1/timer/start", json={}, headers={"X-API-Key": read}
    ).status_code == 403


def test_tracking_scope_writes_time_only(client, raw_client):
    rw = _make_key(client, "read_write")
    pid = _project(raw_client, rw, "TSCOPE")
    tracking = _make_key(client, "tracking")

    # time-entries and timer: allowed
    entry = raw_client.post(
        "/api/v1/time-entries",
        json={"project_id": pid, "entry_date": "2026-06-02", "duration_minutes": 30},
        headers={"X-API-Key": tracking},
    )
    assert entry.status_code == 201, entry.text
    assert raw_client.post(
        "/api/v1/timer/start", json={"project_id": pid}, headers={"X-API-Key": tracking}
    ).status_code == 201
    # clean up the running timer so it can't leak into other tests
    assert raw_client.delete(
        "/api/v1/timer/current", headers={"X-API-Key": tracking}
    ).status_code == 204

    # projects: forbidden for tracking scope
    blocked = raw_client.post(
        "/api/v1/projects",
        json={"name": "Nope", "code": "TS_NOPE"},
        headers={"X-API-Key": tracking},
    )
    assert blocked.status_code == 403


def test_read_write_scope_is_full(client, raw_client):
    rw = _make_key(client, "read_write")
    r = raw_client.post(
        "/api/v1/projects",
        json={"name": "Full", "code": "RWSCOPE"},
        headers={"X-API-Key": rw},
    )
    assert r.status_code == 201


def test_expired_key_is_rejected(client, raw_client):
    from app.db import SessionLocal
    from app.models import ApiKey
    from app.security import generate_api_key

    rw = _make_key(client, "read_write")
    user_id = raw_client.get("/api/v1/auth/me", headers={"X-API-Key": rw}).json()["id"]

    full, prefix, digest = generate_api_key()
    db = SessionLocal()
    try:
        db.add(
            ApiKey(
                user_id=user_id,
                name="expired",
                prefix=prefix,
                key_hash=digest,
                scope="read_write",
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        db.commit()
    finally:
        db.close()

    # Expired key authenticates nothing — even reads → 401.
    assert raw_client.get("/api/v1/auth/me", headers={"X-API-Key": full}).status_code == 401


def test_profile_creates_scoped_key(client, raw_client):
    from tests.conftest import act_as

    act_as(client, "admin@example.com")
    created = client.post(
        "/profile/api-keys",
        data={"name": "RaycastTracking", "scope": "tracking", "expires_in_days": "30"},
        follow_redirects=False,
    )
    assert created.status_code == 302

    page = client.get("/profile").text
    assert "Tracking" in page  # scope badge
    m = re.search(r"(thk_[A-Za-z0-9_\-]+)", page)
    assert m
    key = m.group(1)

    # The tracking key behaves as configured: reads ok, project writes blocked.
    assert raw_client.get("/api/v1/projects", headers={"X-API-Key": key}).status_code == 200
    assert raw_client.post(
        "/api/v1/projects", json={"name": "x", "code": "PFTRK"}, headers={"X-API-Key": key}
    ).status_code == 403
