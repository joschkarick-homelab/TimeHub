import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app.models._enums import EntrySource
from app.schemas.time_entry import BulkResult, TimeEntryBulkCreate
from app.services.csv_import import import_csv

router = APIRouter(prefix="/intake", tags=["intake"])


@router.post("/time-entries", response_model=BulkResult, status_code=201)
def intake_time_entries(
    payload: TimeEntryBulkCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Accept time entries from external tools. Auth via JWT or X-API-Key header."""
    from app.api.time_entries import _create_entry

    created_ids: list[int] = []
    errors: list[dict] = []
    for idx, item in enumerate(payload.entries):
        try:
            entry = _create_entry(db, current_user, item)
            entry.source = EntrySource.API
            db.flush()
            created_ids.append(entry.id)
        except HTTPException as e:
            errors.append({"index": idx, "error": e.detail})
        except Exception as e:  # noqa: BLE001
            errors.append({"index": idx, "error": str(e)})
    db.commit()
    return BulkResult(created=len(created_ids), failed=len(errors), errors=errors, ids=created_ids)


@router.post("/csv", response_model=BulkResult, status_code=201)
async def intake_csv(
    file: UploadFile = File(...),
    mapping: str = Form(..., description="JSON-encoded CsvImportMapping"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        cfg = json.loads(mapping)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"invalid mapping JSON: {e}") from e

    raw = await file.read()
    try:
        result = import_csv(
            db,
            user_id=current_user.id,
            raw_bytes=raw,
            column_map=cfg.get("column_map", {}),
            default_project_code=cfg.get("default_project_code"),
            separator=cfg.get("separator", ";"),
            encoding=cfg.get("encoding", "utf-8"),
            date_format=cfg.get("date_format", "%Y-%m-%d"),
            time_format=cfg.get("time_format", "%H:%M"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return BulkResult(**result)
