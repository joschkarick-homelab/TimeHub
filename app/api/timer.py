from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.time_entries import _create_entry
from app.db import get_db
from app.deps import get_current_user
from app.models import ActiveTimer, Project, TimeEntry, User
from app.schemas.time_entry import TimeEntryCreate, TimeEntryOut
from app.schemas.timer import TimerOut, TimerStart, TimerStop, TimerUpdate
from app.services.sync_rules import load_rules

router = APIRouter(prefix="/timer", tags=["timer"])


def _local_now() -> datetime:
    """Naive local wall-clock time. TimeHub stores user-facing times (entry
    dates, start/end times) as naive local values; the timer follows suit so
    elapsed math and the materialized entry line up with manual entries."""
    return datetime.now()


def _as_local_naive(dt: datetime) -> datetime:
    """Normalize a client-supplied timestamp to naive local time."""
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _elapsed_seconds(timer: ActiveTimer) -> int:
    started = _as_local_naive(timer.started_at)
    return max(0, int((_local_now() - started).total_seconds()))


def timer_to_out(timer: ActiveTimer) -> TimerOut:
    project = timer.project
    return TimerOut(
        id=timer.id,
        project_id=timer.project_id,
        project_code=project.code if project else None,
        project_name=project.name if project else None,
        description=timer.description,
        tags=list(timer.tags or []),
        started_at=timer.started_at,
        elapsed_seconds=_elapsed_seconds(timer),
    )


def get_active_timer(db: Session, user: User) -> ActiveTimer | None:
    return db.execute(
        select(ActiveTimer).where(ActiveTimer.user_id == user.id)
    ).scalar_one_or_none()


def _resolve_project(
    db: Session, user: User, project_id: int | None, project_code: str | None
) -> Project | None:
    """Resolve a project for the user by id or code. Returns None when neither
    is given; raises 400 when one is given but doesn't match an owned project."""
    if project_id is not None:
        project = db.get(Project, project_id)
    elif project_code and project_code.strip():
        project = db.execute(
            select(Project).where(
                Project.user_id == user.id, Project.code == project_code.strip()
            )
        ).scalar_one_or_none()
    else:
        return None
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=400, detail="project not found")
    return project


# ── Core operations (shared by the HTTP API and the MCP server) ──────────────
# These raise HTTPException so the API layer maps them to status codes for free;
# the MCP layer catches it and surfaces the detail as a tool error.


def start_timer_core(db: Session, user: User, payload: TimerStart) -> ActiveTimer:
    if get_active_timer(db, user) is not None:
        raise HTTPException(status_code=409, detail="A timer is already running. Stop it first.")

    # Project is optional — a bare timer can be started and labelled later.
    project = _resolve_project(db, user, payload.project_id, payload.project_code)

    started_at = _as_local_naive(payload.started_at) if payload.started_at else _local_now()
    timer = ActiveTimer(
        user_id=user.id,
        project_id=project.id if project else None,
        description=payload.description or "",
        tags=payload.tags or [],
        started_at=started_at,
    )
    db.add(timer)
    db.commit()
    db.refresh(timer)
    return timer


def update_timer_core(db: Session, user: User, payload: TimerUpdate) -> ActiveTimer:
    """Patch the running timer — assign/change project, description, or tags.
    Only provided fields are touched."""
    timer = get_active_timer(db, user)
    if timer is None:
        raise HTTPException(status_code=404, detail="No timer running")
    project = _resolve_project(db, user, payload.project_id, payload.project_code)
    if project is not None:
        timer.project_id = project.id
    if payload.description is not None:
        timer.description = payload.description
    if payload.tags is not None:
        timer.tags = payload.tags
    db.add(timer)
    db.commit()
    db.refresh(timer)
    return timer


def stop_timer_core(db: Session, user: User, payload: TimerStop) -> TimeEntry:
    timer = get_active_timer(db, user)
    if timer is None:
        raise HTTPException(status_code=404, detail="No timer running")

    # A project can be assigned at stop time; otherwise reuse the timer's.
    project = _resolve_project(db, user, payload.project_id, payload.project_code)
    project_id = project.id if project else timer.project_id
    if project_id is None:
        raise HTTPException(
            status_code=400,
            detail="This timer has no project. Assign one (PATCH /timer/current) "
            "or pass project_code when stopping.",
        )

    started = _as_local_naive(timer.started_at)
    now = _local_now()
    minutes = max(1, round((now - started).total_seconds() / 60))
    if payload.round_to_minutes:
        r = payload.round_to_minutes
        minutes = ((minutes + r - 1) // r) * r

    entry_payload = TimeEntryCreate(
        project_id=project_id,
        entry_date=started.date(),
        start_time=started.time().replace(second=0, microsecond=0),
        end_time=now.time().replace(second=0, microsecond=0),
        duration_minutes=minutes,
        description=payload.description if payload.description is not None else timer.description,
        tags=payload.tags if payload.tags is not None else list(timer.tags or []),
    )
    entry = _create_entry(db, user, entry_payload, load_rules(db))
    db.delete(timer)
    db.commit()
    db.refresh(entry)
    return entry


def cancel_timer_core(db: Session, user: User) -> None:
    timer = get_active_timer(db, user)
    if timer is None:
        raise HTTPException(status_code=404, detail="No timer running")
    db.delete(timer)
    db.commit()


# ── HTTP endpoints ───────────────────────────────────────────────────────────


@router.get("/current", response_model=TimerOut | None)
def current_timer(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    timer = get_active_timer(db, current_user)
    return timer_to_out(timer) if timer else None


@router.post("/start", response_model=TimerOut, status_code=201)
def start_timer(
    payload: TimerStart,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return timer_to_out(start_timer_core(db, current_user, payload))


@router.patch("/current", response_model=TimerOut)
def update_timer(
    payload: TimerUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return timer_to_out(update_timer_core(db, current_user, payload))


@router.post("/stop", response_model=TimeEntryOut, status_code=201)
def stop_timer(
    payload: TimerStop | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return stop_timer_core(db, current_user, payload or TimerStop())


@router.delete("/current", status_code=204)
def cancel_timer(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    cancel_timer_core(db, current_user)
    return Response(status_code=204)
