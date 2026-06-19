import logging
from datetime import date, timedelta

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import EntrySync, ImportFormat, Project, SavedView, TimeEntry, User
from app.services import entry_sync as es_svc
from app.services import reports as report_svc
from app.services import salesforce as sf_svc
from app.services import sync_fields as sf
from app.web.common import (
    DASHBOARD_ENTRY_CAP,
    DATE_RANGES,
    _ctx,
    _filter_query,
    _group_by_day,
    _parse_date,
    _require_login,
    _visible_formats,
    resolve_date_range,
    templates,
)

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    db: Session = Depends(get_db),
    date_range: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    project_id: str | None = None,
    customer: str | None = None,
    view: str | None = None,
    error: str | None = None,
    flash: str | None = None,
):
    user = _require_login(request, db)

    # The user's saved dashboard views, plus the one currently applied (if any).
    saved_views = list(
        db.execute(
            select(SavedView)
            .where(SavedView.user_id == user.id, SavedView.kind == "dashboard")
            .order_by(SavedView.name)
        ).scalars()
    )
    active_view: SavedView | None = None
    if view:
        try:
            vid = int(view)
        except ValueError:
            vid = None
        if vid is not None:
            active_view = next((v for v in saved_views if v.id == vid), None)

    if active_view is not None:
        range_token = active_view.date_range
        df, dt = resolve_date_range(range_token, active_view.date_from, active_view.date_to)
        project_id_int = active_view.project_id
        customer = active_view.customer or ""
    else:
        df = _parse_date(date_from)
        dt = _parse_date(date_to)
        project_id_int = None
        if project_id:
            try:
                project_id_int = int(project_id)
            except ValueError:
                project_id_int = None
        customer = (customer or "").strip()
        # Resolve the range: an explicit token wins; otherwise default to the
        # current week (the list is then tightly bounded to "now"), unless an
        # explicit date / project / customer filter is already in play.
        range_token = date_range if date_range in DATE_RANGES else None
        if range_token is None:
            if df is not None or dt is not None:
                range_token = "custom"
            elif project_id_int is not None or customer:
                range_token = "all"
            else:
                range_token = "this_week"
        df, dt = resolve_date_range(range_token, df, dt)

    stmt = (
        select(TimeEntry)
        .where(TimeEntry.user_id == user.id)
        .options(selectinload(TimeEntry.entry_syncs))
        .order_by(TimeEntry.entry_date.desc(), TimeEntry.id.desc())
    )
    if df is not None:
        stmt = stmt.where(TimeEntry.entry_date >= df)
    if dt is not None:
        stmt = stmt.where(TimeEntry.entry_date <= dt)
    if project_id_int is not None:
        stmt = stmt.where(TimeEntry.project_id == project_id_int)
    if customer:
        stmt = stmt.where(
            TimeEntry.project_id.in_(
                select(Project.id).where(
                    Project.user_id == user.id, Project.customer == customer
                )
            )
        )

    entries = list(db.execute(stmt.limit(DASHBOARD_ENTRY_CAP + 1)).scalars())
    truncated = len(entries) > DASHBOARD_ENTRY_CAP
    entries = entries[:DASHBOARD_ENTRY_CAP]
    days = _group_by_day(entries)

    projects = list(
        db.execute(
            select(Project)
            .where(Project.user_id == user.id, Project.status == "active")
            .order_by(Project.code)
        ).scalars()
    )
    # All of the user's projects (incl. inactive) so sync status resolves for every entry.
    proj_lookup = {
        p.id: p
        for p in db.execute(select(Project).where(Project.user_id == user.id)).scalars()
    }
    projects_by_id = {p.id: p for p in projects}
    customers = sorted({p.customer for p in proj_lookup.values() if p.customer})
    total_minutes = sum(e.duration_minutes for e in entries)

    entry_status = {
        e.id: sf.entry_sync_status(e, proj_lookup[e.project_id])
        for e in entries
        if e.project_id in proj_lookup
    }
    # Per-target traffic-light cells for the dashboard status matrix.
    entry_matrix = {
        e.id: es_svc.matrix_row(e, proj_lookup[e.project_id])
        for e in entries
        if e.project_id in proj_lookup
    }

    sf_configured = sf_svc.credentials_configured(db)
    # Pre-flagged entries the user can pick for a Salesforce sync: target must
    # resolve to salesforce, local data must be sync-ready, and the entry hasn't
    # been synced yet.
    sf_selectable = {
        e.id: (
            entry_status.get(e.id, {}).get("target") == "salesforce"
            and entry_status.get(e.id, {}).get("ready") is True
            and e.sync_status not in ("synced", "manually_synced")
        )
        for e in entries
    }
    sf_selectable_count = sum(1 for v in sf_selectable.values() if v)

    formats = _visible_formats(db, user) if entries else []

    # Salesforce hours synced in the current calendar week (Mon–Sun).
    _today = date.today()
    _week_start = _today - timedelta(days=_today.weekday())
    _week_end = _week_start + timedelta(days=6)
    sf_week_minutes = db.execute(
        select(func.sum(TimeEntry.duration_minutes))
        .join(EntrySync, TimeEntry.id == EntrySync.entry_id)
        .where(
            TimeEntry.user_id == user.id,
            EntrySync.target == "salesforce",
            EntrySync.status.in_(["synced", "manually_synced"]),
            TimeEntry.entry_date >= _week_start,
            TimeEntry.entry_date <= _week_end,
        )
    ).scalar() or 0
    sf_week_hours = round(sf_week_minutes / 60, 1)

    return templates.TemplateResponse(
        "dashboard.html",
        _ctx(
            request,
            user,
            days=days,
            projects=projects,
            projects_by_id=projects_by_id,
            entry_status=entry_status,
            entry_matrix=entry_matrix,
            matrix_targets=[(t, es_svc.TARGET_LABELS[t]) for t in es_svc.DISPLAY_TARGETS],
            sf_selectable=sf_selectable,
            sf_selectable_count=sf_selectable_count,
            sf_configured=sf_configured,
            sync_field_registry=sf.registry_json("entry"),
            project_targets={p.id: p.default_sync_target for p in projects},
            total_hours=round(total_minutes / 60, 2),
            entry_count=len(entries),
            sf_week_hours=sf_week_hours,
            today=date.today().isoformat(),
            date_range=range_token,
            date_ranges=DATE_RANGES,
            date_from=df.isoformat() if df else "",
            date_to=dt.isoformat() if dt else "",
            project_id=project_id_int or "",
            customer=customer or "",
            customers=customers,
            saved_views=saved_views,
            active_view=active_view,
            filter_query=_filter_query(df, dt, project_id_int, customer or None),
            formats=formats,
            truncated=truncated,
            entry_cap=DASHBOARD_ENTRY_CAP,
            error=error,
            flash=flash,
        ),
    )


@router.get("/entries/export", response_class=Response)
def entries_export(
    request: Request,
    db: Session = Depends(get_db),
    # Querystring values come as strings; empty-string "no filter" needs to be
    # tolerated rather than raising 422. We parse manually.
    format_id: str = "",
    date_from: str | None = None,
    date_to: str | None = None,
    project_id: str | None = None,
    customer: str | None = None,
):
    user = _require_login(request, db)

    try:
        fmt_id_int = int(format_id) if format_id else 0
    except ValueError:
        raise HTTPException(status_code=400, detail="format_id must be an integer") from None
    project_id_int: int | None = None
    if project_id:
        try:
            project_id_int = int(project_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="project_id must be an integer") from None

    fmt = db.get(ImportFormat, fmt_id_int) if fmt_id_int else None
    if fmt is None or (not fmt.is_global and fmt.owner_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="format not found")

    df = _parse_date(date_from)
    dt = _parse_date(date_to)
    stmt = (
        select(TimeEntry, Project, User)
        .join(Project, Project.id == TimeEntry.project_id)
        .join(User, User.id == TimeEntry.user_id)
        .where(TimeEntry.user_id == user.id)
        .order_by(TimeEntry.entry_date, TimeEntry.id)
    )
    if df is not None:
        stmt = stmt.where(TimeEntry.entry_date >= df)
    if dt is not None:
        stmt = stmt.where(TimeEntry.entry_date <= dt)
    if project_id_int is not None:
        stmt = stmt.where(TimeEntry.project_id == project_id_int)
    if customer:
        stmt = stmt.where(Project.customer == customer)
    rows = list(db.execute(stmt).all())

    try:
        body, encoding = report_svc.export_via_import_format(
            rows,
            fmt.column_map,
            separator=fmt.separator,
            encoding=fmt.encoding,
            date_format=fmt.date_format,
            time_format=fmt.time_format,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    today = date.today().isoformat()
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in fmt.name)
    filename = f"timehub-{safe_name}-{today}.csv"
    return Response(
        content=body.encode(encoding),
        media_type=f"text/csv; charset={encoding}",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

