from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.time_entries import _create_entry
from app.db import get_db
from app.deps import get_current_user
from app.models import ActiveTimer, Project, User
from app.schemas.time_entry import TimeEntryCreate, TimeEntryOut
from app.schemas.timer import TimerOut, TimerStart, TimerStop
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


def _to_out(timer: ActiveTimer) -> TimerOut:
    return TimerOut(
        id=timer.id,
        project_id=timer.project_id,
        project_code=timer.project.code,
        project_name=timer.project.name,
        description=timer.description,
        tags=list(timer.tags or []),
        started_at=timer.started_at,
        elapsed_seconds=_elapsed_seconds(timer),
    )


def _get_timer(db: Session, user: User) -> ActiveTimer | None:
    return db.execute(
        select(ActiveTimer).where(ActiveTimer.user_id == user.id)
    ).scalar_one_or_none()


@router.get("/current", response_model=TimerOut | None)
def current_timer(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    timer = _get_timer(db, current_user)
    return _to_out(timer) if timer else None


@router.post("/start", response_model=TimerOut, status_code=201)
def start_timer(
    payload: TimerStart,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if _get_timer(db, current_user) is not None:
        raise HTTPException(status_code=409, detail="A timer is already running. Stop it first.")

    # Resolve the project, scoped to the requesting user (projects are per-user).
    if payload.project_id is not None:
        project = db.get(Project, payload.project_id)
    else:
        project = db.execute(
            select(Project).where(
                Project.user_id == current_user.id, Project.code == payload.project_code.strip()
            )
        ).scalar_one_or_none()
    if project is None or project.user_id != current_user.id:
        raise HTTPException(status_code=400, detail="project not found")

    started_at = _as_local_naive(payload.started_at) if payload.started_at else _local_now()
    timer = ActiveTimer(
        user_id=current_user.id,
        project_id=project.id,
        description=payload.description or "",
        tags=payload.tags or [],
        started_at=started_at,
    )
    db.add(timer)
    db.commit()
    db.refresh(timer)
    return _to_out(timer)


@router.post("/stop", response_model=TimeEntryOut, status_code=201)
def stop_timer(
    payload: TimerStop | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    timer = _get_timer(db, current_user)
    if timer is None:
        raise HTTPException(status_code=404, detail="No timer running")
    payload = payload or TimerStop()

    started = _as_local_naive(timer.started_at)
    now = _local_now()
    minutes = max(1, round((now - started).total_seconds() / 60))
    if payload.round_to_minutes:
        r = payload.round_to_minutes
        minutes = ((minutes + r - 1) // r) * r

    entry_payload = TimeEntryCreate(
        project_id=timer.project_id,
        entry_date=started.date(),
        start_time=started.time().replace(second=0, microsecond=0),
        end_time=now.time().replace(second=0, microsecond=0),
        duration_minutes=minutes,
        description=payload.description if payload.description is not None else timer.description,
        tags=payload.tags if payload.tags is not None else list(timer.tags or []),
    )
    entry = _create_entry(db, current_user, entry_payload, load_rules(db))
    db.delete(timer)
    db.commit()
    db.refresh(entry)
    return entry


@router.delete("/current", status_code=204)
def cancel_timer(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    timer = _get_timer(db, current_user)
    if timer is None:
        raise HTTPException(status_code=404, detail="No timer running")
    db.delete(timer)
    db.commit()
    return Response(status_code=204)
