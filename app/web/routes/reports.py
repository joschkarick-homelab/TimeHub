import logging

from fastapi import (
    APIRouter,
    Depends,
    Query,
    Request,
)
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Project, TimeEntry, User
from app.web.common import (
    DATE_RANGES,
    REPORT_ROW_CAP,
    _ctx,
    _require_login,
    load_saved_views,
    resolve_date_range,
    resolve_range_param,
    templates,
)

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    db: Session = Depends(get_db),
    preset: str | None = None,
    group_by: list[str] | None = Query(default=None),
    detailed: str | None = None,
    date_range: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    project_id: str | None = None,
    customer: str | None = None,
    view: str | None = None,
    flash: str | None = None,
    error: str | None = None,
):
    from app.services import report_builder as rb

    user = _require_login(request, db)

    saved_views, active_view = load_saved_views(db, user, "reports", view)
    if active_view is not None:
        # A saved view fully defines grouping + filters; preset is cleared.
        preset = None
        active_group_by = [g for g in active_view.group_by if g in rb.DIMENSIONS]
        active_detailed = active_view.detailed
        range_token = active_view.date_range
        df, dt = resolve_date_range(range_token, active_view.date_from, active_view.date_to)
        pid = active_view.project_id
        customer = active_view.customer or ""
    else:
        # Resolve grouping: an explicit preset wins; else custom group_by + detailed.
        if preset and preset in rb.PRESETS:
            cfg = rb.PRESETS[preset]
            active_group_by = cfg["group_by"]
            active_detailed = cfg["detailed"]
        elif group_by:
            active_group_by = [g for g in group_by if g in rb.DIMENSIONS]
            active_detailed = detailed in ("1", "true", "on", "yes")
            preset = None
        else:
            # default landing view
            preset = "weekly_detailed"
            cfg = rb.PRESETS[preset]
            active_group_by = cfg["group_by"]
            active_detailed = cfg["detailed"]

        # Reports show the full history unless filtered (default range 'all').
        range_token, df, dt = resolve_range_param(date_range, date_from, date_to, default="all")
        pid = None
        if project_id:
            try:
                pid = int(project_id)
            except ValueError:
                pid = None
        customer = (customer or "").strip()

    if not active_group_by:
        active_group_by = ["day"]

    stmt = (
        select(TimeEntry, Project, User)
        .join(Project, Project.id == TimeEntry.project_id)
        .join(User, User.id == TimeEntry.user_id)
        .order_by(TimeEntry.entry_date, TimeEntry.id)
    )
    # Time data is always scoped to the requesting user — admins included.
    stmt = stmt.where(TimeEntry.user_id == user.id)
    if df is not None:
        stmt = stmt.where(TimeEntry.entry_date >= df)
    if dt is not None:
        stmt = stmt.where(TimeEntry.entry_date <= dt)
    if pid is not None:
        stmt = stmt.where(TimeEntry.project_id == pid)
    if customer:
        stmt = stmt.where(Project.customer == customer)

    rows = list(db.execute(stmt.limit(REPORT_ROW_CAP + 1)).all())
    report_truncated = len(rows) > REPORT_ROW_CAP
    rows = rows[:REPORT_ROW_CAP]
    report = rb.build_report(rows, active_group_by, detailed=active_detailed)

    # Project filter is scoped to the user's own projects, mirroring the entries.
    projects = list(
        db.execute(
            select(Project).where(Project.user_id == user.id).order_by(Project.code)
        ).scalars()
    )
    customers = sorted({p.customer for p in projects if p.customer})

    return templates.TemplateResponse(
        "reports.html",
        _ctx(
            request,
            user,
            report=report,
            presets=rb.PRESETS,
            active_preset=preset,
            dimensions=rb.DIMENSION_LABELS,
            active_group_by=active_group_by,
            active_detailed=active_detailed,
            projects=projects,
            customers=customers,
            date_range=range_token,
            date_ranges=DATE_RANGES,
            date_from=df.isoformat() if df else "",
            date_to=dt.isoformat() if dt else "",
            project_id=pid or "",
            customer=customer or "",
            saved_views=saved_views,
            active_view=active_view,
            flash=flash,
            error=error,
            report_truncated=report_truncated,
            report_cap=REPORT_ROW_CAP,
        ),
    )

