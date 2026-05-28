from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import ImportFormat, User
from app.models._enums import EntrySource
from app.schemas.import_format import (
    ImportFormatCreate,
    ImportFormatOut,
    ImportFormatSuggestion,
    ImportFormatUpdate,
)
from app.schemas.time_entry import BulkResult
from app.services.ai_mapping import AiMappingError, suggest_mapping
from app.services.csv_import import import_csv

router = APIRouter(prefix="/import-formats", tags=["import-formats"])


def _visible_to(user: User):
    """Filter clause: formats the given user is allowed to see."""
    return or_(ImportFormat.is_global.is_(True), ImportFormat.owner_id == user.id)


def _ensure_writable(fmt: ImportFormat, user: User) -> None:
    if user.is_admin or fmt.owner_id == user.id:
        return
    raise HTTPException(status_code=403, detail="not allowed to modify this format")


@router.get("", response_model=list[ImportFormatOut])
def list_formats(
    scope: str = "visible",  # visible | mine | global | all (admin)
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(ImportFormat).order_by(ImportFormat.is_global.desc(), ImportFormat.name)
    if scope == "mine":
        stmt = stmt.where(ImportFormat.owner_id == current_user.id)
    elif scope == "global":
        stmt = stmt.where(ImportFormat.is_global.is_(True))
    elif scope == "all":
        if not current_user.is_admin:
            raise HTTPException(status_code=403, detail="admin required for scope=all")
    else:
        stmt = stmt.where(_visible_to(current_user))
    return list(db.execute(stmt).scalars())


@router.post("", response_model=ImportFormatOut, status_code=201)
def create_format(
    payload: ImportFormatCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    fmt = ImportFormat(
        **payload.model_dump(exclude={"is_global"}),
        owner_id=current_user.id,
        is_global=(payload.is_global and current_user.is_admin),
    )
    db.add(fmt)
    db.commit()
    db.refresh(fmt)
    return fmt


@router.get("/{fmt_id}", response_model=ImportFormatOut)
def get_format(
    fmt_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    fmt = db.get(ImportFormat, fmt_id)
    if fmt is None or (not fmt.is_global and fmt.owner_id != current_user.id and not current_user.is_admin):
        raise HTTPException(status_code=404, detail="Not found")
    return fmt


@router.patch("/{fmt_id}", response_model=ImportFormatOut)
def update_format(
    fmt_id: int,
    payload: ImportFormatUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    fmt = db.get(ImportFormat, fmt_id)
    if fmt is None:
        raise HTTPException(status_code=404, detail="Not found")
    _ensure_writable(fmt, current_user)

    data = payload.model_dump(exclude_unset=True)
    if "is_global" in data and not current_user.is_admin:
        data.pop("is_global")
    for field, value in data.items():
        setattr(fmt, field, value)
    db.add(fmt)
    db.commit()
    db.refresh(fmt)
    return fmt


@router.delete("/{fmt_id}", status_code=204)
def delete_format(
    fmt_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    fmt = db.get(ImportFormat, fmt_id)
    if fmt is None:
        raise HTTPException(status_code=404, detail="Not found")
    _ensure_writable(fmt, current_user)
    db.delete(fmt)
    db.commit()
    return None


@router.post("/suggest", response_model=ImportFormatSuggestion)
async def suggest(
    file: UploadFile = File(...),
    _: User = Depends(get_current_user),
):
    """Run a one-shot AI mapping suggestion. The result is NOT persisted —
    show it to the user, let them tweak it, then POST /import-formats to save."""
    raw = await file.read()
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"could not decode file: {e}") from e
    try:
        return suggest_mapping(text)
    except AiMappingError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.post("/{fmt_id}/run", response_model=BulkResult, status_code=201)
async def run_import(
    fmt_id: int,
    file: UploadFile = File(...),
    apply_target_rules: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Apply a saved ImportFormat to an uploaded CSV file."""
    fmt = db.get(ImportFormat, fmt_id)
    if fmt is None or (not fmt.is_global and fmt.owner_id != current_user.id and not current_user.is_admin):
        raise HTTPException(status_code=404, detail="Not found")

    raw = await file.read()
    try:
        result = import_csv(
            db,
            user_id=current_user.id,
            raw_bytes=raw,
            column_map=fmt.column_map,
            default_project_code=fmt.default_project_code,
            separator=fmt.separator,
            encoding=fmt.encoding,
            date_format=fmt.date_format,
            time_format=fmt.time_format,
            transforms=fmt.transforms or [],
            target_rules=fmt.target_rules or [],
            apply_target_rules=apply_target_rules,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # tag the created entries' source as CSV (already done inside import_csv)
    _ = EntrySource.CSV
    return BulkResult(**result)
