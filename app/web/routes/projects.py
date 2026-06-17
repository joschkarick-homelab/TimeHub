import logging

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Request,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Project
from app.services import sync_fields as sf
from app.web.common import (
    _KNOWN_SYNC_TARGETS,
    _ctx,
    _owned_project_or_404,
    _require_login,
    _sync_dynamic_options,
    templates,
)

log = logging.getLogger(__name__)
router = APIRouter()


_KNOWN_STATUSES = ["active", "inactive"]


def _unique_project_code(db: Session, name: str, user_id: int) -> str:
    """Derive a stable, unique code from the project name so users only have to
    enter a name. Normalizes the same way the importer matches codes, so an
    auto-code stays compatible with importing the plain name. Uniqueness is
    per-user, matching the (user_id, code) constraint."""
    import re as _re

    base = _re.sub(r"[^A-Za-z0-9]+", "-", (name or "").strip()).strip("-").upper()[:60] or "PROJEKT"
    existing = {
        c for (c,) in db.execute(
            select(Project.code).where(Project.user_id == user_id)
        ).all()
    }
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


@router.get("/projects", response_class=HTMLResponse)
def projects_page(
    request: Request,
    db: Session = Depends(get_db),
    flash: str | None = None,
    error: str | None = None,
):
    user = _require_login(request, db)
    projects = list(
        db.execute(
            select(Project).where(Project.user_id == user.id).order_by(Project.code)
        ).scalars()
    )
    project_status = {p.id: sf.project_sync_status(p) for p in projects}
    return templates.TemplateResponse(
        "projects.html",
        _ctx(
            request,
            user,
            projects=projects,
            project_status=project_status,
            sync_targets=_KNOWN_SYNC_TARGETS,
            sync_field_registry=sf.registry_json("project"),
            sync_dynamic_options=_sync_dynamic_options(db, user),
            statuses=_KNOWN_STATUSES,
            flash=flash,
            error=error,
        ),
    )


@router.post("/projects", response_class=HTMLResponse)
async def projects_create(
    request: Request,
    name: str = Form(...),
    code: str = Form(""),
    customer: str = Form(""),
    color: str = Form("#6366f1"),
    default_sync_target: str = Form("intern"),
    status_: str = Form("active", alias="status"),
    db: Session = Depends(get_db),
):
    user = _require_login(request, db)
    target = default_sync_target if default_sync_target in _KNOWN_SYNC_TARGETS else "intern"
    # Code is optional — derive a unique one from the name when left blank.
    final_code = code.strip() or _unique_project_code(db, name, user.id)
    p = Project(
        user_id=user.id,
        name=name.strip(),
        code=final_code,
        customer=(customer.strip() or None),
        color=color or "#6366f1",
        default_sync_target=target,
        status=(status_ if status_ in _KNOWN_STATUSES else "active"),
    )
    fields = sf.project_fields(target)
    if fields:
        form = await request.form()
        values = {f.key: form.get(f"meta__{target}__{f.key}", "") for f in fields}
        p.sync_metadata, _ = sf.apply_fields(p.sync_metadata, target, fields, values)
    db.add(p)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url="/projects?error=Projekt-Code+bereits+vergeben",
            status_code=status.HTTP_302_FOUND,
        )
    return RedirectResponse(
        url=f"/projects?flash=Projekt+'{p.code}'+angelegt",
        status_code=status.HTTP_302_FOUND,
    )


@router.get("/projects/{project_id}/edit", response_class=HTMLResponse)
def projects_edit_form(request: Request, project_id: int, db: Session = Depends(get_db)):
    user = _require_login(request, db)
    project = _owned_project_or_404(db, project_id, user)
    return templates.TemplateResponse(
        "project_edit.html",
        _ctx(
            request,
            user,
            project=project,
            sync_targets=_KNOWN_SYNC_TARGETS,
            sync_field_registry=sf.registry_json("project"),
            sync_dynamic_options=_sync_dynamic_options(db, user),
            current_meta=project.sync_metadata or {},
            statuses=_KNOWN_STATUSES,
        ),
    )


@router.post("/projects/{project_id}/edit", response_class=HTMLResponse)
async def projects_update(
    request: Request,
    project_id: int,
    name: str = Form(...),
    code: str = Form(""),
    customer: str = Form(""),
    color: str = Form("#6366f1"),
    default_sync_target: str = Form("intern"),
    status_: str = Form("active", alias="status"),
    db: Session = Depends(get_db),
):
    user = _require_login(request, db)
    project = _owned_project_or_404(db, project_id, user)
    project.name = name.strip()
    # Code optional: keep the existing one when the field is left blank.
    project.code = code.strip() or project.code or _unique_project_code(db, name, user.id)
    project.customer = customer.strip() or None
    project.color = color or "#6366f1"
    target = default_sync_target if default_sync_target in _KNOWN_SYNC_TARGETS else "intern"
    project.default_sync_target = target
    project.status = status_ if status_ in _KNOWN_STATUSES else "active"
    fields = sf.project_fields(target)
    if fields:
        form = await request.form()
        values = {f.key: form.get(f"meta__{target}__{f.key}", "") for f in fields}
        project.sync_metadata, _ = sf.apply_fields(project.sync_metadata, target, fields, values)
    db.add(project)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=f"/projects/{project_id}/edit?error=Projekt-Code+bereits+vergeben",
            status_code=status.HTTP_302_FOUND,
        )
    return RedirectResponse(
        url=f"/projects?flash=Projekt+'{project.code}'+gespeichert",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/projects/{project_id}/delete", response_class=HTMLResponse)
def projects_delete(request: Request, project_id: int, db: Session = Depends(get_db)):
    user = _require_login(request, db)
    project = _owned_project_or_404(db, project_id, user)
    try:
        db.delete(project)
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=f"/projects?error=Projekt+'{project.code}'+hat+Zeiteinträge+und+kann+nicht+gelöscht+werden",
            status_code=status.HTTP_302_FOUND,
        )
    return RedirectResponse(url="/projects", status_code=status.HTTP_302_FOUND)

