"""MCP server: HTTP-layer auth (the ASGI middleware) and the tool logic, which
runs against the real DB scoped to the authenticated user via a context var."""

import pytest

from app import mcp_server as mcp


def _admin_id(client) -> int:
    return client.get("/api/v1/auth/me").json()["id"]


def _project(client, code: str) -> None:
    client.post(
        "/api/v1/projects",
        json={"name": f"MCP {code}", "code": code, "default_sync_target": "intern"},
    )


# ── HTTP auth middleware (Hub X-MSQ identity) ─────────────────────────────────


def test_mcp_requires_identity(raw_client):
    # No X-MSQ-* identity → 401 before any MCP handling (the Hub would normally
    # supply them; a direct request that bypasses the Hub is unauthenticated).
    assert raw_client.post("/mcp/", json={}).status_code == 401


def _drive_middleware(headers: list[tuple[bytes, bytes]]):
    """Run HubIdentityAuthMiddleware around a stub downstream app and capture
    the response start. The stub records that it was reached (delegation) and
    sends a 204; this isolates the AUTH decision from the live MCP session
    manager (whose task group is only running under the app lifespan)."""
    import asyncio

    reached = {"value": False}

    async def downstream(scope, receive, send):
        reached["value"] = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    statuses: list[int] = []

    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    app = mcp.HubIdentityAuthMiddleware(downstream)
    scope = {"type": "http", "method": "POST", "path": "/mcp/", "headers": headers}
    asyncio.run(app(scope, receive, send))
    return statuses[0], reached["value"]


def test_mcp_identity_authenticates_and_delegates(client):
    # An identity-carrying request (X-MSQ-* admin headers) authenticates: the
    # middleware resolves the user, sets the contextvar, and delegates downstream
    # (no 401). The admin user is provisioned by the `client` fixture's identity.
    headers = [
        (b"x-msq-user-id", b"admin-msq"),
        (b"x-msq-user-email", b"admin@example.com"),
        (b"x-msq-user-name", b"Admin"),
    ]
    status, reached = _drive_middleware(headers)
    assert status != 401
    assert reached is True


def test_mcp_no_identity_401_does_not_delegate():
    # No X-MSQ-* headers → 401, downstream MCP app is never reached.
    status, reached = _drive_middleware([])
    assert status == 401
    assert reached is False


# ── Tool logic ────────────────────────────────────────────────────────────────


@pytest.fixture
def as_user(client):
    """Authenticate the tool context as the admin Hub user (as the middleware
    would) and ensure a project exists. The `client` fixture carries the admin
    X-MSQ-* identity, so the user is auto-provisioned on first request."""
    uid = _admin_id(client)
    _project(client, "MCPTOOL")
    token = mcp._user_id_var.set(uid)
    try:
        yield client
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


def test_read_scope_blocks_write_tools(as_user):
    token = mcp._scope_var.set("read")
    try:
        # read tools still work
        assert isinstance(mcp.list_projects(), list)
        # write tools refuse
        with pytest.raises(ValueError, match="read-only"):
            mcp.create_time_entry(project_code="MCPTOOL", duration_minutes=30)
        with pytest.raises(ValueError, match="read-only"):
            mcp.start_timer(project_code="MCPTOOL")
    finally:
        mcp._scope_var.reset(token)


def test_cancel_timer_via_tools(as_user):
    mcp.start_timer(project_code="MCPTOOL")
    assert mcp.cancel_timer() == "Timer cancelled."
    assert mcp.get_current_timer() is None
    with pytest.raises(ValueError, match="No timer running"):
        mcp.cancel_timer()
