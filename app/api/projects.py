from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import Project, User
from app.schemas.project import ProjectCreate, ProjectOut, ProjectUpdate
from app.services import entry_sync as es_svc

router = APIRouter(prefix="/projects", tags=["projects"])


def _owned_or_404(db: Session, project_id: int, user: User) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(
    status: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Project).where(Project.user_id == user.id).order_by(Project.code)
    if status:
        stmt = stmt.where(Project.status == status)
    return list(db.execute(stmt).scalars())


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    payload: ProjectCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    project = Project(**payload.model_dump(), user_id=user.id)
    db.add(project)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail="project code already exists") from e
    db.refresh(project)
    return project


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    return _owned_or_404(db, project_id, user)


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: int,
    payload: ProjectUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = _owned_or_404(db, project_id, user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    db.add(project)
    # Re-open entries stuck on a stale failed status so a corrected project
    # (e.g. a fixed Salesforce assignment) is picked up by the sync again.
    es_svc.reset_open_syncs_for_project(db, project)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail="project code already exists") from e
    db.refresh(project)
    return project


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    project = _owned_or_404(db, project_id, user)
    db.delete(project)
    db.commit()
    return None
