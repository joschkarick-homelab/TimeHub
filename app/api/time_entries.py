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
from app.services.entry_sync import reconcile_entry_syncs
from app.services.sync_rules import load_rules

router = APIRouter(prefix="/time-entries", tags=["time-entries"])


def _build_filter_stmt(
    *,
    current_user: User,
    date_from: date | None,
    date_to: date | None,
    project_id: int | None,
    sync_target: str | None,
    tag: str | None,
):
    stmt = select(TimeEntry).order_by(TimeEntry.entry_date.desc(), TimeEntry.id.desc())
    # Time data is always scoped to the requesting user — admins included.
    stmt = stmt.where(TimeEntry.user_id == current_user.id)
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
        sync_target=sync_target,
        tag=tag,
    ).limit(limit)
    items = list(db.execute(stmt).scalars())
    if tag:
        items = [e for e in items if tag in (e.tags or [])]
    return items


def _create_entry(
    db: Session, current_user: User, payload: TimeEntryCreate, rules=()
) -> TimeEntry:
    # Entries always belong to the requesting user — nobody (incl. admins) may
    # create on behalf of another user.
    if payload.user_id is not None and payload.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="cannot create entries for other users")
    target_user_id = current_user.id

    project = db.get(Project, payload.project_id)
    # Projects are per-user: an entry may only reference its owner's project.
    if project is None or project.user_id != target_user_id:
        raise HTTPException(status_code=400, detail=f"project {payload.project_id} not found")

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
    db.flush()  # assign id so sync rows can reference it
    reconcile_entry_syncs(db, entry, project, rules)
    return entry


@router.post("", response_model=TimeEntryOut, status_code=201)
def create_time_entry(
    payload: TimeEntryCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    entry = _create_entry(db, current_user, payload, load_rules(db))
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
    rules = load_rules(db)
    for idx, item in enumerate(payload.entries):
        try:
            entry = _create_entry(db, current_user, item, rules)
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
    if entry is None or entry.user_id != current_user.id:
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
    if entry is None or entry.user_id != current_user.id:
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
    db.flush()
    reconcile_entry_syncs(db, entry, entry.project, load_rules(db))
    db.commit()
    db.refresh(entry)
    return entry


@router.delete("/{entry_id}", status_code=204)
def delete_entry(
    entry_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    entry = db.get(TimeEntry, entry_id)
    if entry is None or entry.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(entry)
    db.commit()
    return None
