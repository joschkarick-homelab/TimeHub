"""MCP server: HTTP-layer auth (the ASGI middleware) and the tool logic, which
runs against the real DB scoped to the authenticated user via a context var."""

import pytest

from app import mcp_server as mcp


def _api_key(client) -> str:
    token = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "testpass"},
    ).json()["access_token"]
    return client.post(
        "/api/v1/auth/api-keys",
        json={"name": "mcp-test"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()["key"]


def _admin_id(client, key: str) -> int:
    return client.get("/api/v1/auth/me", headers={"X-API-Key": key}).json()["id"]


def _project(client, key: str, code: str) -> None:
    client.post(
        "/api/v1/projects",
        json={"name": f"MCP {code}", "code": code, "default_sync_target": "intern"},
        headers={"X-API-Key": key},
    )


# ── HTTP auth middleware ──────────────────────────────────────────────────────


def test_mcp_requires_auth(client):
    # No key → 401 before any MCP handling.
    assert client.post("/mcp/", json={}).status_code == 401
    # Bogus key → 401 too.
    assert client.post("/mcp/", json={}, headers={"X-API-Key": "thk_nope"}).status_code == 401


# ── Tool logic ────────────────────────────────────────────────────────────────


@pytest.fixture
def as_user(client):
    """Authenticate the tool context as the admin user (as the middleware would)
    and ensure a project exists."""
    key = _api_key(client)
    uid = _admin_id(client, key)
    _project(client, key, "MCPTOOL")
    token = mcp._user_id_var.set(uid)
    try:
        yield key
    finally:
        mcp._user_id_var.reset(token)


def test_tools_unauthenticated_raise():
    # No context var set → tools refuse.
    with pytest.raises(ValueError, match="Not authenticated"):
        mcp.list_projects()


def test_list_projects_includes_created(as_user):
    codes = {p["code"] for p in mcp.list_projects()}
    assert "MCPTOOL" in codes


def test_create_time_entry_and_weekly(as_user):
    entry = mcp.create_time_entry(
        project_code="MCPTOOL", duration_minutes=30, description="via mcp"
    )
    assert entry["duration_minutes"] == 30
    assert entry["description"] == "via mcp"

    week = mcp.get_weekly_hours()
    assert week["total_minutes"] >= 30
    assert any(p["code"] == "MCPTOOL" for p in week["by_project"])


def test_create_time_entries_bulk_reports_errors(as_user):
    result = mcp.create_time_entries(
        [
            mcp.EntryInput(project_code="MCPTOOL", duration_minutes=15, description="a"),
            mcp.EntryInput(project_code="DOES_NOT_EXIST", duration_minutes=15),
        ]
    )
    assert result["created"] == 1
    assert result["failed"] == 1
    assert result["errors"][0]["index"] == 1


def test_create_entry_unknown_project_raises(as_user):
    with pytest.raises(ValueError, match="No project with code"):
        mcp.create_time_entry(project_code="NOPE", duration_minutes=10)


def test_timer_lifecycle_via_tools(as_user):
    started = mcp.start_timer(project_code="MCPTOOL", description="focus")
    assert started["project_code"] == "MCPTOOL"

    assert mcp.get_current_timer()["id"] == started["id"]

    # Second start refused while one runs.
    with pytest.raises(ValueError, match="already running"):
        mcp.start_timer(project_code="MCPTOOL")

    entry = mcp.stop_timer(round_to_minutes=15)
    assert entry["duration_minutes"] >= 1
    assert mcp.get_current_timer() is None


def test_bare_timer_assign_then_stop_via_tools(as_user):
    started = mcp.start_timer()  # no project
    assert started["project_id"] is None

    mcp.update_timer(project_code="MCPTOOL", description="set later")
    assert mcp.get_current_timer()["project_code"] == "MCPTOOL"

    entry = mcp.stop_timer()
    assert entry["description"] == "set later"


def test_stop_assigns_project_inline_via_tools(as_user):
    mcp.start_timer()
    entry = mcp.stop_timer(project_code="MCPTOOL")
    assert entry["project_id"]


def test_cancel_timer_via_tools(as_user):
    mcp.start_timer(project_code="MCPTOOL")
    assert mcp.cancel_timer() == "Timer cancelled."
    assert mcp.get_current_timer() is None
    with pytest.raises(ValueError, match="No timer running"):
        mcp.cancel_timer()
