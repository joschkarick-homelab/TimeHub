from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import CsvTemplate, Project, TimeEntry, User
from app.schemas.report import WeeklyHours, WeeklyProjectHours, WeeklyTargetHours
from app.services import reports as report_svc

router = APIRouter(prefix="/reports", tags=["reports"])

FORMATS = {"json", "csv", "markdown", "md"}


def week_bounds(week_offset: int = 0) -> tuple[date, date]:
    """Monday–Sunday bounds for the current week, shifted by ``week_offset``."""
    today = date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    return monday, monday + timedelta(days=6)


def compute_weekly(
    db: Session, current_user: User, date_from: date, date_to: date
) -> WeeklyHours:
    """Aggregate tracked time over a date range — total plus per-project and
    per-effective-target breakdowns. Shared by the HTTP endpoint and the MCP
    server."""
    rows = list(
        db.execute(
            select(TimeEntry, Project)
            .join(Project, Project.id == TimeEntry.project_id)
            .where(
                TimeEntry.user_id == current_user.id,
                TimeEntry.entry_date >= date_from,
                TimeEntry.entry_date <= date_to,
            )
        ).all()
    )

    total = 0
    by_project: dict[int, dict] = {}
    by_target: dict[str, int] = {}
    for entry, project in rows:
        mins = entry.duration_minutes or 0
        total += mins
        proj = by_project.setdefault(
            project.id,
            {"project_id": project.id, "code": project.code, "name": project.name, "minutes": 0},
        )
        proj["minutes"] += mins
        target = entry.sync_target_override or project.default_sync_target
        by_target[target] = by_target.get(target, 0) + mins

    return WeeklyHours(
        date_from=date_from,
        date_to=date_to,
        total_minutes=total,
        total_hours=round(total / 60, 2),
        entry_count=len(rows),
        by_project=sorted(
            (
                WeeklyProjectHours(**p, hours=round(p["minutes"] / 60, 2))
                for p in by_project.values()
            ),
            key=lambda p: p.minutes,
            reverse=True,
        ),
        by_target=sorted(
            (
                WeeklyTargetHours(target=t, minutes=m, hours=round(m / 60, 2))
                for t, m in by_target.items()
            ),
            key=lambda t: t.minutes,
            reverse=True,
        ),
    )


@router.get("/weekly", response_model=WeeklyHours)
def weekly_hours(
    week_offset: int = Query(0, description="0 = current week, -1 = last week, etc."),
    date_from: date | None = Query(None, description="Override week start (inclusive)"),
    date_to: date | None = Query(None, description="Override week end (inclusive)"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Tracked time for a week — total plus per-project and per-effective-target
    breakdowns. Defaults to the current Mon–Sun week; ``week_offset`` shifts it,
    or pass an explicit ``date_from``/``date_to``."""
    monday, sunday = week_bounds(week_offset)
    return compute_weekly(db, current_user, date_from or monday, date_to or sunday)


def _gather(
    db: Session,
    current_user: User,
    *,
    date_from: date | None,
    date_to: date | None,
    project_id: int | None,
    sync_target: str | None,
    tag: str | None,
):
    stmt = (
        select(TimeEntry, Project, User)
        .join(Project, Project.id == TimeEntry.project_id)
        .join(User, User.id == TimeEntry.user_id)
        .order_by(TimeEntry.entry_date, TimeEntry.id)
    )
    # Time data is always scoped to the requesting user — admins included.
    stmt = stmt.where(TimeEntry.user_id == current_user.id)
    if date_from is not None:
        stmt = stmt.where(TimeEntry.entry_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(TimeEntry.entry_date <= date_to)
    if project_id is not None:
        stmt = stmt.where(TimeEntry.project_id == project_id)
    if sync_target is not None:
        stmt = stmt.where(
            (TimeEntry.sync_target_override == sync_target)
            | (
                (TimeEntry.sync_target_override.is_(None))
                & (Project.default_sync_target == sync_target)
            )
        )

    rows = list(db.execute(stmt).all())
    if tag:
        rows = [r for r in rows if tag in (r[0].tags or [])]
    return rows


@router.get("/timesheet")
def timesheet(
    format: str = Query("json", pattern="^(json|csv|markdown|md)$"),
    date_from: date | None = None,
    date_to: date | None = None,
    project_id: int | None = None,
    sync_target: str | None = None,
    tag: str | None = None,
    csv_template_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = _gather(
        db,
        current_user,
        date_from=date_from,
        date_to=date_to,
        project_id=project_id,
        sync_target=sync_target,
        tag=tag,
    )

    if format == "json":
        return Response(content=report_svc.to_json(rows), media_type="application/json")

    if format in {"markdown", "md"}:
        return Response(content=report_svc.to_markdown(rows), media_type="text/markdown")

    template: CsvTemplate | None = None
    if csv_template_id is not None:
        template = db.get(CsvTemplate, csv_template_id)
        if template is None:
            raise HTTPException(status_code=404, detail="csv_template not found")
    body, encoding = report_svc.to_csv(rows, template)
    return Response(
        content=body.encode(encoding),
        media_type=f"text/csv; charset={encoding}",
        headers={"Content-Disposition": 'attachment; filename="timesheet.csv"'},
    )
