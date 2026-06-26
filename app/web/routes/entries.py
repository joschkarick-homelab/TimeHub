import logging
from datetime import date

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Request,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Project, TimeEntry
from app.services import entry_sync as es_svc
from app.services import sync_fields as sf
from app.services.sync_rules import load_rules
from app.web.common import (
    _KNOWN_SYNC_TARGETS,
    _ctx,
    _owned_entry_or_404,
    _owned_project_or_404,
    _parse_duration_minutes,
    _parse_time,
    _require_login,
    _resolve_duration,
    _safe_next,
    templates,
)

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/entries", response_class=HTMLResponse)
async def create_entry(
    request: Request,
    entry_date: str = Form(...),
    project_id: int = Form(...),
    duration_minutes: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    description: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _require_login(request, db)
    project = _owned_project_or_404(db, project_id, user)
    back = _safe_next(next)
    start = _parse_time(start_time)
    end = _parse_time(end_time)
    try:
        duration = _resolve_duration(start, end, _parse_duration_minutes(duration_minutes))
    except ValueError as e:
        sep = "&" if "?" in back else "?"
        return RedirectResponse(
            url=f"{back}{sep}error={e}".replace(" ", "+"),
            status_code=status.HTTP_302_FOUND,
        )
    entry = TimeEntry(
        user_id=user.id,
        project_id=project_id,
        entry_date=date.fromisoformat(entry_date),
        start_time=start,
        end_time=end,
        duration_minutes=duration,
        description=description,
    )
    form = await request.form()
    sync_targets_list = list(form.getlist("sync_targets_override"))
    valid_targets = [t for t in sync_targets_list if t in _KNOWN_SYNC_TARGETS
                     and t not in ("intern", "none")]
    if valid_targets:
        entry.sync_targets_override = valid_targets
    effective = valid_targets if valid_targets else [project.default_sync_target]
    for target in effective:
        fields = sf.entry_fields(target)
        if fields:
            values = {f.key: form.get(f"meta__{target}__{f.key}", "") for f in fields}
            entry.sync_metadata_override, _ = sf.apply_fields(
                entry.sync_metadata_override, target, fields, values
            )
    db.add(entry)
    db.flush()
    es_svc.reconcile_entry_syncs(db, entry, project, load_rules(db))
    db.commit()
    return RedirectResponse(url=back, status_code=status.HTTP_302_FOUND)


@router.get("/entries/{entry_id}/edit", response_class=HTMLResponse)
def edit_entry_form(
    request: Request, entry_id: int, db: Session = Depends(get_db),
    error: str | None = None, next: str | None = None,
    modal: str | None = None,
):
    user = _require_login(request, db)
    entry = _owned_entry_or_404(db, entry_id, user)
    projects = list(
        db.execute(
            select(Project).where(Project.user_id == user.id).order_by(Project.code)
        ).scalars()
    )
    template = "_entry_form.html" if modal else "entry_edit.html"
    return templates.TemplateResponse(
        template,
        _ctx(
            request,
            user,
            entry=entry,
            projects=projects,
            next_url=_safe_next(next),
            sync_targets=_KNOWN_SYNC_TARGETS,
            sync_field_registry=sf.registry_json("entry"),
            project_targets={p.id: p.default_sync_target for p in projects},
            project_multi_targets={
                p.id: [t for t in (p.sync_targets if p.sync_targets else [p.default_sync_target])
                       if t not in ("intern", "none")]
                for p in projects
            },
            project_sf_assignments={
                p.id: (p.sync_metadata or {}).get("salesforce", {}).get("assignment_id", "")
                for p in projects
            },
            current_meta=entry.sync_metadata_override or {},
            error=error,
        ),
    )


@router.post("/entries/{entry_id}/edit", response_class=HTMLResponse)
async def edit_entry_submit(
    request: Request,
    entry_id: int,
    entry_date: str = Form(...),
    project_id: int = Form(...),
    duration_minutes: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    description: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _require_login(request, db)
    entry = _owned_entry_or_404(db, entry_id, user)
    project = _owned_project_or_404(db, project_id, user)
    next_url = _safe_next(next)
    start = _parse_time(start_time)
    end = _parse_time(end_time)
    try:
        duration = _resolve_duration(start, end, _parse_duration_minutes(duration_minutes))
    except ValueError as e:
        return RedirectResponse(
            url=f"/entries/{entry_id}/edit?error={e}&next={next_url}".replace(" ", "+"),
            status_code=status.HTTP_302_FOUND,
        )
    entry.entry_date = date.fromisoformat(entry_date)
    entry.project_id = project_id
    entry.start_time = start
    entry.end_time = end
    entry.duration_minutes = duration
    entry.description = description

    form = await request.form()
    sync_targets_list = list(form.getlist("sync_targets_override"))
    valid_targets = [t for t in sync_targets_list if t in _KNOWN_SYNC_TARGETS
                     and t not in ("intern", "none")]
    entry.sync_target_override = None
    entry.sync_targets_override = valid_targets if valid_targets else None
    effective = valid_targets if valid_targets else [project.default_sync_target]
    for target in effective:
        fields = sf.entry_fields(target)
        if fields:
            values = {f.key: form.get(f"meta__{target}__{f.key}", "") for f in fields}
            entry.sync_metadata_override, _ = sf.apply_fields(
                entry.sync_metadata_override, target, fields, values
            )
    db.add(entry)
    db.flush()
    es_svc.reconcile_entry_syncs(db, entry, project, load_rules(db))
    db.commit()
    return RedirectResponse(url=next_url, status_code=status.HTTP_302_FOUND)


@router.post("/entries/{entry_id}/delete", response_class=HTMLResponse)
def delete_entry(
    request: Request, entry_id: int, next: str = Form(""), db: Session = Depends(get_db)
):
    user = _require_login(request, db)
    entry = _owned_entry_or_404(db, entry_id, user)
    db.delete(entry)
    db.commit()
    return RedirectResponse(url=_safe_next(next), status_code=status.HTTP_302_FOUND)


@router.post("/entries/bulk-delete", response_class=HTMLResponse)
def bulk_delete_entries(
    request: Request,
    entry_ids: list[int] = Form(default_factory=list),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    """Delete several entries at once (mass-select mode on the dashboard).
    Scoped to the user's own entries; the active filter is preserved via next."""
    user = _require_login(request, db)
    back = _safe_next(next)
    if not entry_ids:
        sep = "&" if "?" in back else "?"
        return RedirectResponse(url=f"{back}{sep}error=Keine+Einträge+ausgewählt",
                                status_code=status.HTTP_302_FOUND)
    stmt = select(TimeEntry).where(
        TimeEntry.id.in_(entry_ids), TimeEntry.user_id == user.id,
    )
    n = 0
    for e in db.execute(stmt).scalars():
        db.delete(e)
        n += 1
    db.commit()
    sep = "&" if "?" in back else "?"
    return RedirectResponse(url=f"{back}{sep}flash={n}+Einträge+gelöscht",
                            status_code=status.HTTP_302_FOUND)


@router.post("/entries/mark-synced", response_class=HTMLResponse)
def mark_entries_manually_synced(
    request: Request,
    entry_ids: list[int] = Form(default_factory=list),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    """Markiere die ausgewählten Einträge als 'manuell erfasst' (sync_status=
    manually_synced). Damit verschwinden sie aus der Sync-Auswahl und werden
    auch vom Stapel-Push übersprungen — gedacht für alte Monate, die schon
    direkt in Salesforce erfasst wurden. Der aktive Dashboard-Filter bleibt
    über `next` erhalten."""
    user = _require_login(request, db)
    back = _safe_next(next)
    if not entry_ids:
        sep = "&" if "?" in back else "?"
        return RedirectResponse(url=f"{back}{sep}error=Keine+Einträge+ausgewählt",
                                status_code=status.HTTP_302_FOUND)
    stmt = select(TimeEntry).where(
        TimeEntry.id.in_(entry_ids), TimeEntry.user_id == user.id,
    )
    n = 0
    for e in db.execute(stmt).scalars():
        if e.sync_status in ("synced", "manually_synced"):
            continue
        e.sync_status = "manually_synced"
        es_svc.mark_all_manually_synced(db, e)
        db.add(e)
        n += 1
    db.commit()
    sep = "&" if "?" in back else "?"
    return RedirectResponse(url=f"{back}{sep}flash={n}+Einträge+als+manuell+erfasst+markiert",
                            status_code=status.HTTP_302_FOUND)


@router.post("/entries/{entry_id}/unmark-synced", response_class=HTMLResponse)
def unmark_entry_manually_synced(
    request: Request, entry_id: int, next: str = Form(""), db: Session = Depends(get_db),
):
    """Rückgängig: zurück auf pending. Nur erlaubt, wenn der Eintrag manuell
    markiert war (echte Salesforce-Syncs lassen sich hier nicht zurücksetzen)."""
    user = _require_login(request, db)
    entry = _owned_entry_or_404(db, entry_id, user)
    if entry.sync_status == "manually_synced":
        entry.sync_status = "pending"
        es_svc.unmark_manually_synced(db, entry)
        db.add(entry)
        db.commit()
    return RedirectResponse(url=_safe_next(next), status_code=status.HTTP_302_FOUND)

