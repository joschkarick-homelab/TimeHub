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
    REPORT_ROW_CAP,
    _ctx,
    _parse_date,
    _require_login,
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
    date_from: str | None = None,
    date_to: str | None = None,
    project_id: str | None = None,
    customer: str | None = None,
):
    from app.services import report_builder as rb

    user = _require_login(request, db)

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
    if not active_group_by:
        active_group_by = ["day"]

    df = _parse_date(date_from)
    dt = _parse_date(date_to)
    pid: int | None = None
    if project_id:
        try:
            pid = int(project_id)
        except ValueError:
            pid = None

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
            date_from=df.isoformat() if df else "",
            date_to=dt.isoformat() if dt else "",
            project_id=pid or "",
            customer=customer or "",
            report_truncated=report_truncated,
            report_cap=REPORT_ROW_CAP,
        ),
    )

