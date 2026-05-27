from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import Project, TimeEntry, User
from app.models._enums import EntrySource
from app.schemas.time_entry import (
    BulkResult,
    TimeEntryBulkCreate,
    TimeEntryCreate,
    TimeEntryOut,
    TimeEntryUpdate,
)

router = APIRouter(prefix="/time-entries", tags=["time-entries"])


def _build_filter_stmt(
    *,
    current_user: User,
    date_from: date | None,
    date_to: date | None,
    project_id: int | None,
    user_id: int | None,
    sync_target: str | None,
    tag: str | None,
):
    stmt = select(TimeEntry).order_by(TimeEntry.entry_date.desc(), TimeEntry.id.desc())
    if not current_user.is_admin:
        stmt = stmt.where(TimeEntry.user_id == current_user.id)
    elif user_id is not None:
        stmt = stmt.where(TimeEntry.user_id == user_id)
    if date_from is not None:
        stmt = stmt.where(TimeEntry.entry_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(TimeEntry.entry_date <= date_to)
    if project_id is not None:
        stmt = stmt.where(TimeEntry.project_id == project_id)
    if sync_target is not None:
        stmt = stmt.join(Project, Project.id == TimeEntry.project_id).where(
            (TimeEntry.sync_target_override == sync_target)
            | (
                (TimeEntry.sync_target_override.is_(None))
                & (Project.default_sync_target == sync_target)
            )
        )
    if tag is not None:
        # JSON contains is database-dependent; fall back to in-memory filtering below
        pass
    return stmt


@router.get("", response_model=list[TimeEntryOut])
def list_time_entries(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    project_id: int | None = Query(default=None),
    user_id: int | None = Query(default=None),
    sync_target: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    limit: int = Query(default=500, le=5000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = _build_filter_stmt(
        current_user=current_user,
        date_from=date_from,
        date_to=date_to,
        project_id=project_id,
        user_id=user_id,
        sync_target=sync_target,
        tag=tag,
    ).limit(limit)
    items = list(db.execute(stmt).scalars())
    if tag:
        items = [e for e in items if tag in (e.tags or [])]
    return items


def _create_entry(db: Session, current_user: User, payload: TimeEntryCreate) -> TimeEntry:
    project = db.get(Project, payload.project_id)
    if project is None:
        raise HTTPException(status_code=400, detail=f"project {payload.project_id} not found")

    target_user_id = payload.user_id or current_user.id
    if target_user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="cannot create entries for other users")

    entry = TimeEntry(
        user_id=target_user_id,
        project_id=payload.project_id,
        entry_date=payload.entry_date,
        start_time=payload.start_time,
        end_time=payload.end_time,
        duration_minutes=payload.duration_minutes,
        description=payload.description,
        tags=payload.tags,
        sync_target_override=payload.sync_target_override,
        sync_metadata_override=payload.sync_metadata_override,
        external_ref=payload.external_ref,
        source=EntrySource.MANUAL,
    )
    db.add(entry)
    return entry


@router.post("", response_model=TimeEntryOut, status_code=201)
def create_time_entry(
    payload: TimeEntryCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    entry = _create_entry(db, current_user, payload)
    db.commit()
    db.refresh(entry)
    return entry


@router.post("/bulk", response_model=BulkResult, status_code=201)
def bulk_create(
    payload: TimeEntryBulkCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    created_ids: list[int] = []
    errors: list[dict] = []
    for idx, item in enumerate(payload.entries):
        try:
            entry = _create_entry(db, current_user, item)
            db.flush()
            created_ids.append(entry.id)
        except HTTPException as e:
            errors.append({"index": idx, "error": e.detail})
        except Exception as e:  # noqa: BLE001
            errors.append({"index": idx, "error": str(e)})
    db.commit()
    return BulkResult(created=len(created_ids), failed=len(errors), errors=errors, ids=created_ids)


@router.get("/{entry_id}", response_model=TimeEntryOut)
def get_entry(
    entry_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    entry = db.get(TimeEntry, entry_id)
    if entry is None or (entry.user_id != current_user.id and not current_user.is_admin):
        raise HTTPException(status_code=404, detail="Not found")
    return entry


@router.patch("/{entry_id}", response_model=TimeEntryOut)
def update_entry(
    entry_id: int,
    payload: TimeEntryUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    entry = db.get(TimeEntry, entry_id)
    if entry is None or (entry.user_id != current_user.id and not current_user.is_admin):
        raise HTTPException(status_code=404, detail="Not found")
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(entry, field, value)
    # Recompute duration if missing but interval provided
    if entry.duration_minutes is None and entry.start_time and entry.end_time:
        entry.duration_minutes = (entry.end_time.hour * 60 + entry.end_time.minute) - (
            entry.start_time.hour * 60 + entry.start_time.minute
        )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@router.delete("/{entry_id}", status_code=204)
def delete_entry(
    entry_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    entry = db.get(TimeEntry, entry_id)
    if entry is None or (entry.user_id != current_user.id and not current_user.is_admin):
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(entry)
    db.commit()
    return None
