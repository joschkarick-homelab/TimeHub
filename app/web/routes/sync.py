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
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import Project, TimeEntry
from app.services import entry_sync as es_svc
from app.services import salesforce as sf_svc
from app.services import sf_push
from app.services import sync_fields as sf
from app.web.common import (
    _ctx,
    _owned_entry_or_404,
    _owned_project_or_404,
    _require_admin,
    _require_login,
    _sync_dynamic_options,
    _visible_formats,
    templates,
)

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/sync", response_class=HTMLResponse)
def sync_center(
    request: Request, db: Session = Depends(get_db),
    flash: str | None = None, error: str | None = None,
):
    """Export-Wizard hub: one actionable card per target, fed by the
    materialized EntrySync rows. Salesforce delegates to the live preview/
    execute flow; Jira/BCS can be ticked off as manually handled until their
    push clients land. Blocked entries are listed with a correction deep-link."""
    user = _require_login(request, db)
    entries = list(
        db.execute(
            select(TimeEntry)
            .where(TimeEntry.user_id == user.id)
            .options(selectinload(TimeEntry.entry_syncs))
            .order_by(TimeEntry.entry_date, TimeEntry.id)
        ).scalars()
    )
    proj_lookup = {
        p.id: p for p in db.execute(select(Project).where(Project.user_id == user.id)).scalars()
    }
    buckets = es_svc.wizard_buckets(entries, proj_lookup)

    formats = _visible_formats(db, user)
    return templates.TemplateResponse(
        "sync_center.html",
        _ctx(
            request,
            user,
            buckets=buckets,
            cards=[(t, es_svc.TARGET_LABELS[t]) for t in es_svc.DISPLAY_TARGETS],
            projects_by_id=proj_lookup,
            sf_configured=sf_svc.credentials_configured(db),
            sync_dynamic_options=_sync_dynamic_options(db, user),
            formats=formats,
            flash=flash,
            error=error,
        ),
    )


@router.post("/sync/project/{project_id}/fields", response_class=HTMLResponse)
async def sync_fill_project_fields(
    request: Request, project_id: int, target: str = Form(...), db: Session = Depends(get_db)
):
    """Fill missing project-level sync fields (e.g. the Salesforce assignment)
    straight from the wizard — unblocks every entry of that project at once."""
    user = _require_login(request, db)
    project = _owned_project_or_404(db, project_id, user)
    if target not in es_svc.DISPLAY_TARGETS:
        return RedirectResponse(url="/sync?error=Unbekanntes+Ziel",
                                status_code=status.HTTP_302_FOUND)
    form = await request.form()
    # Only touch fields actually submitted, so unrendered ones aren't cleared.
    submitted = [f for f in sf.project_fields(target) if f"meta__{target}__{f.key}" in form]
    values = {f.key: form.get(f"meta__{target}__{f.key}", "") for f in submitted}
    project.sync_metadata, warnings = sf.apply_fields(
        project.sync_metadata, target, submitted, values
    )
    db.add(project)
    db.commit()
    return _sync_redirect_with_warnings(warnings, "Projekt-Daten gespeichert")


@router.post("/sync/entry/{entry_id}/fields", response_class=HTMLResponse)
async def sync_fill_entry_fields(
    request: Request, entry_id: int, target: str = Form(...), db: Session = Depends(get_db)
):
    """Fill missing entry-level sync fields (e.g. a Jira ticket, BCS subject)
    inline in the wizard."""
    user = _require_login(request, db)
    entry = _owned_entry_or_404(db, entry_id, user)
    if target not in es_svc.DISPLAY_TARGETS:
        return RedirectResponse(url="/sync?error=Unbekanntes+Ziel",
                                status_code=status.HTTP_302_FOUND)
    form = await request.form()
    submitted = [f for f in sf.entry_fields(target) if f"meta__{target}__{f.key}" in form]
    values = {f.key: form.get(f"meta__{target}__{f.key}", "") for f in submitted}
    entry.sync_metadata_override, warnings = sf.apply_fields(
        entry.sync_metadata_override, target, submitted, values
    )
    db.add(entry)
    db.commit()
    return _sync_redirect_with_warnings(warnings, "Eintrags-Daten gespeichert")


def _sync_redirect_with_warnings(warnings: list[str], ok_msg: str) -> RedirectResponse:
    if warnings:
        msg = "; ".join(warnings)
        return RedirectResponse(url=f"/sync?error={msg}".replace(" ", "+"),
                                status_code=status.HTTP_302_FOUND)
    return RedirectResponse(url=f"/sync?flash={ok_msg}".replace(" ", "+"),
                            status_code=status.HTTP_302_FOUND)


@router.post("/sync/{target}/mark-done", response_class=HTMLResponse)
def sync_mark_target_done(
    request: Request,
    target: str,
    entry_ids: list[int] = Form(default_factory=list),
    db: Session = Depends(get_db),
):
    """Tick off one target for the selected entries (EntrySync → manually_synced).
    Used for targets without a live push, and as the wizard's 'abnicken' action."""
    user = _require_login(request, db)
    if target not in es_svc.DISPLAY_TARGETS:
        return RedirectResponse(url="/sync?error=Unbekanntes+Ziel",
                                status_code=status.HTTP_302_FOUND)
    if not entry_ids:
        return RedirectResponse(url="/sync?error=Keine+Einträge+ausgewählt",
                                status_code=status.HTTP_302_FOUND)
    stmt = (
        select(TimeEntry)
        .where(TimeEntry.id.in_(entry_ids), TimeEntry.user_id == user.id)
        .options(selectinload(TimeEntry.entry_syncs))
    )
    n = 0
    for e in db.execute(stmt).scalars():
        if es_svc.mark_target_done(db, e, target):
            n += 1
    db.commit()
    label = es_svc.TARGET_LABELS.get(target, target)
    return RedirectResponse(
        url=f"/sync?flash={n}+Einträge+für+{label}+als+erledigt+markiert",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/sync/salesforce/preview", response_class=HTMLResponse)
def sync_salesforce_preview(
    request: Request,
    entry_ids: list[int] = Form(default_factory=list),
    db: Session = Depends(get_db),
):
    """Resolve the selected entries against Salesforce (read-only) and render
    a preview of the Zeiterfassung__c-Payloads that would be written. No DML.

    Modell der mindsquare-Org: eine Zeiterfassung__c pro TimeHub-Eintrag, mit
    Lookup auf den Kontierungsmonat__c, der wiederum zur Projektbesetzung__c
    gehört. Wir gruppieren die Vorschau nach (Projektbesetzung × Kontierungsmonat)
    zur Übersicht; geschrieben würde aber jeder Eintrag einzeln."""
    user = _require_login(request, db)
    if not entry_ids:
        return RedirectResponse(
            url="/?error=Keine+Einträge+ausgewählt",
            status_code=status.HTTP_302_FOUND,
        )

    # Restrict to the user's own entries (admin sees only their own here too —
    # syncing on behalf of others requires explicit assignment data we don't
    # have yet).
    stmt = (
        select(TimeEntry)
        .where(TimeEntry.id.in_(entry_ids), TimeEntry.user_id == user.id)
        .order_by(TimeEntry.entry_date, TimeEntry.id)
    )
    entries = list(db.execute(stmt).scalars())
    if not entries:
        return RedirectResponse(
            url="/?error=Keine+gültigen+Einträge+gefunden",
            status_code=status.HTTP_302_FOUND,
        )
    proj_lookup = {
        p.id: p for p in db.execute(select(Project).where(Project.user_id == user.id)).scalars()
    }

    client = sf_svc.client_from_settings(db)
    if client is None:
        return templates.TemplateResponse(
            "sync_salesforce_preview.html",
            _ctx(request, user, error=(
                "Salesforce-Zugangsdaten sind nicht hinterlegt. "
                "Admin: unter Nutzer → Salesforce-Integration eintragen."
            ), groups=[], errors=[], entries=entries),
        )

    # Resolve every entry against Salesforce (shared with the execute flow) and
    # group the pushable ones by (Projektbesetzung × Kontierungsmonat).
    resolved, sf_error = sf_push.resolve_pushes(client, entries, proj_lookup)
    grouped: dict[tuple[str, str], dict] = {}
    skipped: list[dict] = []
    for r in resolved:
        if r["status"] == "blocked":
            skipped.append({"entry": r["entry"], "reason": r["reason"]})
            continue
        group = grouped.setdefault((r["assignment_id"], r["period"]["id"]), {
            "assignment": r["assignment"],
            "period": r["period"],
            "entries": [],
            "total_hours": 0.0,
        })
        group["entries"].append({
            "entry": r["entry"],
            "payload": r["payload"],
            "remote_value": r["remote_value"] or "",
        })
        group["total_hours"] = round(
            group["total_hours"] + r["entry"].duration_minutes / 60.0, 2
        )

    groups = list(grouped.values())
    pushable_count = sum(len(g["entries"]) for g in groups)

    return templates.TemplateResponse(
        "sync_salesforce_preview.html",
        _ctx(
            request,
            user,
            groups=groups,
            skipped=skipped,
            item_errors=[],
            sf_error=sf_error,
            entries=entries,
            pushable_count=pushable_count,
            error=None,
        ),
    )


@router.post("/sync/salesforce/execute", response_class=HTMLResponse)
def sync_salesforce_execute(
    request: Request,
    entry_ids: list[int] = Form(default_factory=list),
    db: Session = Depends(get_db),
):
    """Pushe die ausgewählten Einträge tatsächlich nach Salesforce. Pro Eintrag
    wird vor dem Insert nochmal voll validiert (PB, Kontierungsmonat-Status etc.);
    bereits synchronisierte Einträge werden idempotent übersprungen. Pro-Eintrag-
    Fehler brechen den Rest nicht ab."""
    user = _require_login(request, db)
    if not entry_ids:
        return RedirectResponse(url="/?error=Keine+Einträge+ausgewählt",
                                status_code=status.HTTP_302_FOUND)

    client = sf_svc.client_from_settings(db)
    if client is None:
        return templates.TemplateResponse(
            "sync_salesforce_execute.html",
            _ctx(request, user, results=[], sf_error=None, error=(
                "Salesforce-Zugangsdaten sind nicht hinterlegt. "
                "Admin: unter Nutzer → Salesforce-Integration eintragen."
            )),
        )

    stmt = (
        select(TimeEntry)
        .where(TimeEntry.id.in_(entry_ids), TimeEntry.user_id == user.id)
        .order_by(TimeEntry.entry_date, TimeEntry.id)
    )
    entries = list(db.execute(stmt).scalars())
    proj_lookup = {
        p.id: p for p in db.execute(select(Project).where(Project.user_id == user.id)).scalars()
    }
    results: list[dict] = []
    to_resolve: list[TimeEntry] = []

    def _fail(e, error: str) -> None:
        e.sync_status = "failed"
        es_svc.set_target_status(db, e, "salesforce", "failed", error=error)
        db.add(e)
        db.commit()
        results.append({"entry": e, "status": "failed", "error": error})

    # Idempotency: already-synced entries are skipped without touching Salesforce.
    for entry in entries:
        if entry.sync_status in ("synced", "manually_synced"):
            existing = ((entry.sync_metadata_override or {}).get("salesforce") or {}).get("zeiterfassung_id")
            reason = ("bereits manuell als erfasst markiert"
                      if entry.sync_status == "manually_synced"
                      else "bereits synchronisiert")
            results.append({"entry": entry, "status": "skipped",
                            "reason": reason, "id": existing})
        else:
            to_resolve.append(entry)

    # Same read-only resolution the preview uses — then POST the pushable ones.
    resolved, sf_error = sf_push.resolve_pushes(client, to_resolve, proj_lookup)
    for r in resolved:
        entry = r["entry"]
        if r["status"] == "blocked":
            _fail(entry, r["reason"])
            continue
        try:
            new_id = sf_svc.create_zeiterfassung(client, r["payload"])
        except sf_svc.SalesforceError as e:
            _fail(entry, str(e))
            continue

        # Persist id + sync_status. JSON column → assign a fresh dict.
        meta = dict(entry.sync_metadata_override or {})
        sf_meta = dict(meta.get("salesforce") or {})
        sf_meta["zeiterfassung_id"] = new_id
        meta["salesforce"] = sf_meta
        entry.sync_metadata_override = meta
        entry.sync_status = "synced"
        es_svc.set_target_status(db, entry, "salesforce", "synced", external_ref=new_id)
        db.add(entry)
        # Commit per entry: the Salesforce record already exists at this point,
        # so the remote id must be persisted atomically with the POST's success.
        # A later crash/timeout in the loop then can't strand a synced record
        # without its id and cause a duplicate on the next push.
        db.commit()
        results.append({"entry": entry, "status": "synced", "id": new_id,
                        "assignment": r["assignment"], "period": r["period"],
                        "warning": sf_svc.duration_snap_warning(entry.duration_minutes)})

    synced = sum(1 for r in results if r["status"] == "synced")
    failed = sum(1 for r in results if r["status"] == "failed")
    skipped_count = sum(1 for r in results if r["status"] == "skipped")
    return templates.TemplateResponse(
        "sync_salesforce_execute.html",
        _ctx(
            request,
            user,
            results=results,
            sf_error=sf_error,
            synced=synced,
            failed=failed,
            skipped=skipped_count,
            instance_url=client.instance_url,
            error=None,
        ),
    )


@router.post("/admin/salesforce/describe", response_class=HTMLResponse)
def admin_salesforce_describe(
    request: Request,
    object_names: str = Form(""),
    db: Session = Depends(get_db),
):
    """Schema-Inspektor: ruft pro angegebenem sObject die Describe-Metadaten
    ab und rendert die wesentlichen Felder. Hilft beim Anpassen an Custom
    Objects (keine Admin-Anmeldung in Salesforce nötig)."""
    user = _require_admin(request, db)
    client = sf_svc.client_from_settings(db)
    if client is None:
        return RedirectResponse(
            url="/users?error=Bitte+zuerst+Salesforce-Zugangsdaten+hinterlegen",
            status_code=status.HTTP_302_FOUND,
        )

    raw_names = [n.strip() for n in object_names.replace(",", "\n").splitlines()]
    names = [n for n in raw_names if n]
    if not names:
        return RedirectResponse(
            url="/users?error=Mindestens+einen+sObject-Namen+eintragen",
            status_code=status.HTTP_302_FOUND,
        )

    results: list[dict] = []
    for n in names:
        try:
            meta = sf_svc.describe_sobject(client, n)
        except sf_svc.SalesforceError as e:
            results.append({"name": n, "error": str(e)})
            continue
        fields = []
        for f in meta.get("fields", []):
            picklist = [
                {"value": p.get("value"), "label": p.get("label")}
                for p in (f.get("picklistValues") or [])
                if p.get("active")
            ][:25]
            fields.append({
                "name": f.get("name"),
                "label": f.get("label"),
                "type": f.get("type"),
                "length": f.get("length"),
                "nillable": f.get("nillable"),
                "custom": f.get("custom"),
                "referenceTo": f.get("referenceTo") or [],
                "relationshipName": f.get("relationshipName"),
                "picklistValues": picklist,
            })
        results.append({
            "name": meta.get("name") or n,
            "label": meta.get("label"),
            "custom": meta.get("custom"),
            "fields": fields,
        })

    return templates.TemplateResponse(
        "admin_sf_describe.html",
        _ctx(request, user, results=results),
    )

