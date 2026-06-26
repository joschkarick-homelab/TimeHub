"""MCP server for TimeHub, mounted at ``/mcp`` (Streamable HTTP).

Lets an MCP client (Claude Desktop / Claude Code) read projects and write time
entries — including driving the same server-side timer the Raycast extension
uses. It is a thin layer over the existing API core functions, so behaviour and
validation stay identical across the HTTP API, Raycast, and MCP.

Behind the Agent Hub the MCP server runs in ``mcp-bearer`` mode: the Hub does
the M365/Entra OAuth, strips the inbound ``Authorization`` header, and forwards
the same ``X-MSQ-*`` identity headers the rest of the app uses. A pure-ASGI
middleware resolves the user once per request from those headers (or the
dev-bypass identity) and stashes the id in a context variable the tools read —
the MCP server itself does no token/API-key auth, mirroring the web/API readers.
"""

import contextvars
import functools
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import date

from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.reports import compute_weekly, week_bounds
from app.api.time_entries import _create_entry
from app.api.timer import (
    cancel_timer_core,
    get_active_timer,
    start_timer_core,
    stop_timer_core,
    timer_to_out,
    update_timer_core,
)
from app.config import get_settings
from app.db import SessionLocal
from app.identity import _dev_principal, principal_from_headers, resolve_user
from app.models import Project, User
from app.schemas.time_entry import TimeEntryCreate, TimeEntryOut
from app.schemas.timer import TimerStart, TimerStop, TimerUpdate

# Resolved per request by the auth middleware; tools read it to scope all work
# to the authenticated user.
_user_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "mcp_user_id", default=None
)
# Access scope for the request. Hub identity always resolves to read_write; the
# 'read' guard in _require_write is kept for future-proofing. Write tools require
# a non-read scope.
_scope_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "mcp_scope", default="read_write"
)


def _require_write() -> None:
    """Guard for write tools: a read-only key may not mutate anything. All MCP
    write tools touch only time-entries/timer, so the 'tracking' scope passes."""
    if _scope_var.get() == "read":
        raise ValueError("This API key is read-only; it cannot create or change time entries.")

mcp = FastMCP("TimeHub", stateless_http=True, json_response=True, streamable_http_path="/")


@contextmanager
def _session_user() -> Iterator[tuple[Session, User]]:
    """A DB session plus the authenticated user for the current request."""
    user_id = _user_id_var.get()
    if user_id is None:
        raise ValueError("Not authenticated")
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            raise ValueError("Not authenticated")
        yield db, user
    finally:
        db.close()


def _tool(fn):
    """Register ``fn`` as an MCP tool, translating the API core's HTTPException
    into a plain tool error so the client sees a clean message. Returns the
    wrapped callable so tests can invoke tools directly."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except HTTPException as exc:
            raise ValueError(str(exc.detail)) from exc

    mcp.tool()(wrapper)
    return wrapper


class EntryInput(BaseModel):
    """One time entry for the bulk tool."""

    project_code: str = Field(description="Code of an existing project (see list_projects).")
    duration_minutes: int = Field(ge=1, description="Logged duration in minutes.")
    description: str = ""
    entry_date: str | None = Field(default=None, description="YYYY-MM-DD; defaults to today.")
    tags: list[str] = Field(default_factory=list)
    start_time: str | None = Field(default=None, description="Optional HH:MM.")
    end_time: str | None = Field(default=None, description="Optional HH:MM.")


def _project_by_code(db: Session, user: User, code: str) -> Project:
    project = db.execute(
        select(Project).where(Project.user_id == user.id, Project.code == code.strip())
    ).scalar_one_or_none()
    if project is None:
        raise ValueError(f"No project with code '{code}'. Use list_projects to see valid codes.")
    return project


def _make_entry(db: Session, user: User, data: EntryInput, rules) -> TimeEntryOut:
    project = _project_by_code(db, user, data.project_code)
    payload = TimeEntryCreate(
        project_id=project.id,
        entry_date=data.entry_date or date.today().isoformat(),
        duration_minutes=data.duration_minutes,
        description=data.description,
        tags=data.tags,
        start_time=data.start_time,
        end_time=data.end_time,
    )
    entry = _create_entry(db, user, payload, rules)
    return entry


# ── Tools ────────────────────────────────────────────────────────────────────


@_tool
def list_projects() -> list[dict]:
    """List the user's active projects (code, name, customer, default sync
    target). Use the returned `code` when creating entries or starting a timer."""
    with _session_user() as (db, user):
        projects = db.execute(
            select(Project)
            .where(Project.user_id == user.id, Project.status == "active")
            .order_by(Project.name)
        ).scalars()
        return [
            {
                "code": p.code,
                "name": p.name,
                "customer": p.customer,
                "default_sync_target": p.default_sync_target,
            }
            for p in projects
        ]


@_tool
def create_time_entry(
    project_code: str,
    duration_minutes: int,
    description: str = "",
    entry_date: str | None = None,
    tags: list[str] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> dict:
    """Log a single time entry. `project_code` must match an existing project,
    `duration_minutes` is the logged duration, `entry_date` is YYYY-MM-DD
    (defaults to today). `start_time`/`end_time` are optional HH:MM labels."""
    from app.services.sync_rules import load_rules

    _require_write()
    with _session_user() as (db, user):
        out = _make_entry(
            db,
            user,
            EntryInput(
                project_code=project_code,
                duration_minutes=duration_minutes,
                description=description,
                entry_date=entry_date,
                tags=tags or [],
                start_time=start_time,
                end_time=end_time,
            ),
            load_rules(db),
        )
        db.commit()
        return TimeEntryOut.model_validate(out).model_dump(mode="json")


@_tool
def create_time_entries(entries: list[EntryInput]) -> dict:
    """Log several time entries at once. Returns counts plus the created ids and
    any per-row errors (a bad row does not abort the others)."""
    from app.services.sync_rules import load_rules

    _require_write()
    with _session_user() as (db, user):
        rules = load_rules(db)
        created_ids: list[int] = []
        errors: list[dict] = []
        for idx, item in enumerate(entries):
            try:
                out = _make_entry(db, user, item, rules)
                db.flush()
                created_ids.append(out.id)
            except (ValueError, HTTPException) as exc:
                detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                errors.append({"index": idx, "error": str(detail)})
        db.commit()
        return {"created": len(created_ids), "failed": len(errors), "ids": created_ids, "errors": errors}


@_tool
def get_current_timer() -> dict | None:
    """Return the running timer (project, description, started_at,
    elapsed_seconds) or null if none is running."""
    with _session_user() as (db, user):
        timer = get_active_timer(db, user)
        return timer_to_out(timer).model_dump(mode="json") if timer else None


@_tool
def start_timer(
    project_code: str | None = None, description: str = "", tags: list[str] | None = None
) -> dict:
    """Start a timer. All fields are optional — a bare timer can be started and
    its project assigned later (update_timer) or when stopping. Fails if one is
    already running."""
    _require_write()
    with _session_user() as (db, user):
        timer = start_timer_core(
            db, user, TimerStart(project_code=project_code, description=description, tags=tags or [])
        )
        return timer_to_out(timer).model_dump(mode="json")


@_tool
def update_timer(
    project_code: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Assign or change the running timer's project, description, or tags. Only
    provided fields change."""
    _require_write()
    with _session_user() as (db, user):
        timer = update_timer_core(
            db, user, TimerUpdate(project_code=project_code, description=description, tags=tags)
        )
        return timer_to_out(timer).model_dump(mode="json")


@_tool
def stop_timer(round_to_minutes: int | None = None, project_code: str | None = None) -> dict:
    """Stop the running timer and create the time entry. `project_code` assigns
    a project if the timer was started without one. `round_to_minutes`
    optionally rounds the duration up to the nearest step (e.g. 15)."""
    _require_write()
    with _session_user() as (db, user):
        entry = stop_timer_core(
            db, user, TimerStop(round_to_minutes=round_to_minutes, project_code=project_code)
        )
        return TimeEntryOut.model_validate(entry).model_dump(mode="json")


@_tool
def cancel_timer() -> str:
    """Discard the running timer without creating an entry."""
    _require_write()
    with _session_user() as (db, user):
        cancel_timer_core(db, user)
        return "Timer cancelled."


@_tool
def get_weekly_hours(week_offset: int = 0) -> dict:
    """Tracked time for a week (total, per project, per sync target).
    week_offset 0 = current week, -1 = last week, etc."""
    with _session_user() as (db, user):
        monday, sunday = week_bounds(week_offset)
        return compute_weekly(db, user, monday, sunday).model_dump(mode="json")


# ── Auth middleware + ASGI wiring ─────────────────────────────────────────────


async def _send_401(send) -> None:
    body = b'{"detail":"Not authenticated"}'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _ScopeHeaders:
    """Minimal ``.get()`` over the raw ASGI header list for principal_from_headers."""

    def __init__(self, raw):
        self._h = {k.decode().lower(): v.decode("latin-1") for k, v in raw}

    def get(self, key, default=None):
        return self._h.get(key.lower(), default)


class HubIdentityAuthMiddleware:
    """Pure-ASGI guard (not BaseHTTPMiddleware, which would break the SSE
    stream): resolve identity from X-MSQ-* (or dev-bypass), set the user
    contextvar, then delegate to the MCP app. The Hub (mcp-bearer) has already
    done the OAuth and stripped Authorization, so we never see a token. Rejects
    requests without a resolvable, active user with 401."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if get_settings().resolved_auth_mode == "dev-bypass":
            principal = _dev_principal()
        else:
            principal = principal_from_headers(_ScopeHeaders(scope.get("headers", [])))
        if principal is None:
            await _send_401(send)
            return
        db = SessionLocal()
        try:
            user = resolve_user(db, principal)
            user_id = user.id if user.is_active else None
        finally:
            db.close()
        if user_id is None:
            await _send_401(send)
            return
        user_token = _user_id_var.set(user_id)
        scope_token = _scope_var.set("read_write")
        try:
            await self.app(scope, receive, send)
        finally:
            _user_id_var.reset(user_token)
            _scope_var.reset(scope_token)


_asgi_app = None


def build_asgi_app():
    """The auth-wrapped MCP ASGI app (built once)."""
    global _asgi_app
    if _asgi_app is None:
        _asgi_app = HubIdentityAuthMiddleware(mcp.streamable_http_app())
    return _asgi_app


_session_started = False


@asynccontextmanager
async def session_lifespan():
    """Run the MCP session manager for the app's lifetime; main.app enters this
    from its own lifespan when MCP is enabled. The session manager may only be
    run once per process, so guard against repeated lifespans (e.g. a test
    suite that spins up many TestClients)."""
    global _session_started
    build_asgi_app()  # ensure the session manager exists
    if _session_started:
        yield
        return
    _session_started = True
    async with mcp.session_manager.run():
        yield
