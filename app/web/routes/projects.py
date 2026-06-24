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
from app.services import entry_sync as es_svc
from app.services import salesforce as sf_svc
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


# Distinct, dark-theme-friendly hues handed out to freshly imported projects so
# they're easy to tell apart on the dashboard (Tailwind-500 palette).
_IMPORT_COLORS = [
    "#6366f1", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444",
    "#8b5cf6", "#ec4899", "#14b8a6", "#84cc16", "#f97316",
]


def _next_import_color(taken: set[str], index: int) -> str:
    """First palette colour not already in use, so imports don't all look alike
    and tend to differ from existing projects too. Once the palette is
    exhausted it cycles by `index` (the running import count) so a batch still
    spreads across the palette instead of repeating one hue."""
    for c in _IMPORT_COLORS:
        if c not in taken:
            return c
    return _IMPORT_COLORS[index % len(_IMPORT_COLORS)]


def _distinct_customers(db: Session, user_id: int) -> list[str]:
    """Alphabetisch sortierte, eindeutige Kundennamen aus den Projekten des
    Users — Grundlage für die Autocomplete-Vorschläge in den Projektformularen."""
    rows = db.execute(
        select(Project.customer)
        .where(Project.user_id == user_id, Project.customer.isnot(None))
        .distinct()
    ).all()
    return sorted({c.strip() for (c,) in rows if c and c.strip()})


def _existing_sf_assignment_ids(db: Session, user_id: int) -> set[str]:
    """Salesforce-Projektbesetzungs-Ids, die bereits an einem Projekt des Users
    hinterlegt sind — Grundlage dafür, schon importierte PBs auszublenden."""
    ids: set[str] = set()
    for (md,) in db.execute(
        select(Project.sync_metadata).where(Project.user_id == user_id)
    ).all():
        aid = ((md or {}).get("salesforce") or {}).get("assignment_id")
        if aid:
            ids.add(aid)
    return ids


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
            customers=_distinct_customers(db, user.id),
            sync_targets=_KNOWN_SYNC_TARGETS,
            sync_field_registry=sf.registry_json("project"),
            sync_dynamic_options=_sync_dynamic_options(db, user),
            sf_configured=sf_svc.credentials_configured(db),
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


@router.get("/projects/import-salesforce", response_class=HTMLResponse)
def projects_import_sf_form(
    request: Request, db: Session = Depends(get_db), error: str | None = None
):
    """Liste der aktuell laufenden Salesforce-Projektbesetzungen des Users
    (gleiche Filter wie das PB-Dropdown), beschränkt auf jene, die noch an
    keinem Projekt hinterlegt sind — zum Ankreuzen und Anlegen."""
    user = _require_login(request, db)
    if not sf_svc.credentials_configured(db):
        return RedirectResponse(
            url="/projects?error=Salesforce+ist+nicht+konfiguriert",
            status_code=status.HTTP_302_FOUND,
        )
    client = sf_svc.client_from_settings(db)
    assignments: list[dict] = []
    sf_error: str | None = None
    if client is None or not user.email:
        sf_error = "Keine Salesforce-Verbindung oder fehlende E-Mail-Adresse."
    else:
        try:
            existing = _existing_sf_assignment_ids(db, user.id)
            assignments = [
                a
                for a in sf_svc.assignments_for_import(client, user.email)
                if a["assignment_id"] not in existing
            ]
        except sf_svc.SalesforceError as exc:
            log.info("SF project import lookup failed: %s", exc)
            sf_error = f"Salesforce-Abfrage fehlgeschlagen: {exc}"
    return templates.TemplateResponse(
        "projects_import_sf.html",
        _ctx(request, user, assignments=assignments, sf_error=sf_error, error=error),
    )


@router.post("/projects/import-salesforce", response_class=HTMLResponse)
async def projects_import_sf_run(request: Request, db: Session = Depends(get_db)):
    """Legt für jede angekreuzte Projektbesetzung ein Projekt an (Ziel:
    salesforce, mit hinterlegter assignment_id). Re-fetch + Re-Dedup, damit eine
    inzwischen angelegte oder weggefallene PB nicht doppelt/fälschlich landet."""
    user = _require_login(request, db)
    if not sf_svc.credentials_configured(db):
        return RedirectResponse(
            url="/projects?error=Salesforce+ist+nicht+konfiguriert",
            status_code=status.HTTP_302_FOUND,
        )
    form = await request.form()
    selected = set(form.getlist("assignment_ids"))
    if not selected:
        return RedirectResponse(
            url="/projects?error=Keine+Projektbesetzung+ausgewählt",
            status_code=status.HTTP_302_FOUND,
        )
    client = sf_svc.client_from_settings(db)
    if client is None or not user.email:
        return RedirectResponse(
            url="/projects?error=Keine+Salesforce-Verbindung",
            status_code=status.HTTP_302_FOUND,
        )
    try:
        available = {
            a["assignment_id"]: a
            for a in sf_svc.assignments_for_import(client, user.email)
        }
    except sf_svc.SalesforceError as exc:
        log.info("SF project import failed: %s", exc)
        return RedirectResponse(
            url="/projects?error=Salesforce-Abfrage+fehlgeschlagen",
            status_code=status.HTTP_302_FOUND,
        )

    existing = _existing_sf_assignment_ids(db, user.id)
    taken_colors = {
        (c or "").lower()
        for (c,) in db.execute(
            select(Project.color).where(Project.user_id == user.id)
        ).all()
        if c
    }
    sf_fields = sf.project_fields("salesforce")
    created = 0
    for aid in selected:
        a = available.get(aid)
        if a is None or aid in existing:
            continue  # stale selection or imported in the meantime
        base = a["number"] or a["name"]
        color = _next_import_color(taken_colors, created)
        taken_colors.add(color)
        p = Project(
            user_id=user.id,
            name=(a["name"] or base)[:255],
            code=_unique_project_code(db, base, user.id),
            customer=(a["customer"] or None),
            color=color,
            default_sync_target="salesforce",
            status="active",
        )
        p.sync_metadata, _ = sf.apply_fields(
            {}, "salesforce", sf_fields, {"assignment_id": aid}
        )
        db.add(p)
        db.flush()  # so the next _unique_project_code sees this code
        existing.add(aid)
        created += 1

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url="/projects?error=Import+fehlgeschlagen+(Code-Kollision)",
            status_code=status.HTTP_302_FOUND,
        )
    if created == 0:
        return RedirectResponse(
            url="/projects?flash=Keine+neuen+Projekte+angelegt",
            status_code=status.HTTP_302_FOUND,
        )
    return RedirectResponse(
        url=f"/projects?flash={created}+Projekt(e)+aus+Salesforce+angelegt",
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
            customers=_distinct_customers(db, user.id),
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
    # Re-open entries stuck on a stale failed status so a corrected project
    # (e.g. a fixed Salesforce assignment) is picked up by the sync again.
    es_svc.reset_open_syncs_for_project(db, project)
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

