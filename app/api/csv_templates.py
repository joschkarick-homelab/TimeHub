from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, require_admin
from app.models import CsvTemplate, User
from app.schemas.csv_template import CsvTemplateCreate, CsvTemplateOut, CsvTemplateUpdate

router = APIRouter(prefix="/csv-templates", tags=["csv-templates"])


@router.get("", response_model=list[CsvTemplateOut])
def list_templates(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return list(db.execute(select(CsvTemplate).order_by(CsvTemplate.name)).scalars())


@router.post("", response_model=CsvTemplateOut, status_code=201)
def create_template(
    payload: CsvTemplateCreate,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    data = payload.model_dump()
    data["columns"] = [c for c in data["columns"]]
    tpl = CsvTemplate(**data)
    db.add(tpl)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail="template name already exists") from e
    db.refresh(tpl)
    return tpl


@router.get("/{tpl_id}", response_model=CsvTemplateOut)
def get_template(tpl_id: int, _: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tpl = db.get(CsvTemplate, tpl_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="Not found")
    return tpl


@router.patch("/{tpl_id}", response_model=CsvTemplateOut)
def update_template(
    tpl_id: int,
    payload: CsvTemplateUpdate,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    tpl = db.get(CsvTemplate, tpl_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="Not found")
    data = payload.model_dump(exclude_unset=True)
    if "columns" in data and data["columns"] is not None:
        data["columns"] = [c for c in data["columns"]]
    for field, value in data.items():
        setattr(tpl, field, value)
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


@router.delete("/{tpl_id}", status_code=204)
def delete_template(
    tpl_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)
):
    tpl = db.get(CsvTemplate, tpl_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(tpl)
    db.commit()
    return None
