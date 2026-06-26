import json
import logging
from datetime import date, timedelta

from fastapi import (
    APIRouter,
    Depends,
    Request,
)
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import M365Connection, Project, TimeEntry
from app.services import entry_sync as es_svc
from app.services import m365 as m365_svc
from app.services import sync_fields as sf
from app.services.sync_rules import load_rules
from app.web.common import (
    _ctx,
    _json_user_or_401,
    _parse_date,
    _parse_time,
    _require_login,
    _resolve_duration,
    templates,
)

log = logging.getLogger(__name__)
router = APIRouter()


# Vertical scale of the day grid. The template and the drag JS both derive
# from this single constant so they stay in sync.
CAL_PX_PER_HOUR = 44


_WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _minutes(t) -> int | None:
    return t.hour * 60 + t.minute if t else None


def _cal_entry(e: TimeEntry, project: Project | None) -> dict:
    status = (
        sf.entry_sync_status(e, project)
        if project is not None
        else {"ready": True, "needs_sync": False, "missing": []}
    )
    return {
        "id": e.id,
        "project_id": e.project_id,
        "project_label": project.display_label if project else "Unbekanntes Projekt",
        "color": project.color if project else "#6366f1",
        "description": e.description or "",
        "start": _minutes(e.start_time),
        "end": _minutes(e.end_time),
        "duration_minutes": e.duration_minutes,
        "ready": status["ready"],
        "needs_sync": status["needs_sync"],
        "missing": status.get("missing", []),
    }


@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(
    request: Request,
    db: Session = Depends(get_db),
    start: str | None = None,
    days: str | None = None,
    error: str | None = None,
):
    user = _require_login(request, db)

    try:
        days_n = int(days) if days else 7
    except ValueError:
        days_n = 7
    days_n = max(1, min(7, days_n))

    start_d = _parse_date(start)
    if start_d is None:
        today = date.today()
        # Week view aligns to Monday; shorter ranges start on today.
        start_d = today - timedelta(days=today.weekday()) if days_n == 7 else today
    end_d = start_d + timedelta(days=days_n - 1)

    stmt = (
        select(TimeEntry)
        .where(
            TimeEntry.user_id == user.id,
            TimeEntry.entry_date >= start_d,
            TimeEntry.entry_date <= end_d,
        )
        .order_by(TimeEntry.start_time)
    )
    entries = list(db.execute(stmt).scalars())

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

    # Optional read-only Microsoft 365 calendar overlay. Any failure (lapsed
    # consent, network, Graph error) degrades to a banner — time tracking stays
    # fully usable.
    m365_conn = db.execute(
        select(M365Connection).where(M365Connection.user_id == user.id)
    ).scalar_one_or_none()
    m365_events: list[dict] = []
    m365_error: str | None = None
    if m365_conn is not None:
        try:
            m365_events = m365_svc.calendar_view(db, m365_conn, start_d, end_d)
        except m365_svc.M365Error as e:
            m365_error = str(e)
            # Persist the error so the profile page can prompt a reconnect.
            if m365_conn.last_error != m365_error:
                m365_conn.last_error = m365_error
                db.add(m365_conn)
                db.commit()
            log.info("M365 calendar fetch failed for user %s: %s", user.id, e)

    today = date.today()
    columns = []
    for i in range(days_n):
        d = start_d + timedelta(days=i)
        day_entries = [e for e in entries if e.entry_date == d]
        timed, untimed = [], []
        for e in day_entries:
            payload = _cal_entry(e, proj_lookup.get(e.project_id))
            (timed if payload["start"] is not None and payload["end"] is not None else untimed).append(payload)
        columns.append({
            "date": d.isoformat(),
            "weekday": _WEEKDAYS_DE[d.weekday()],
            "label": d.strftime("%d.%m."),
            "is_today": d == today,
            "timed": timed,
            "untimed": untimed,
            "total_minutes": sum(e.duration_minutes for e in day_entries),
            "m365": (
                m365_svc.events_for_day(m365_events, d)
                if m365_conn is not None
                else {"timed": [], "allday": []}
            ),
        })

    projects_json = [
        {"id": p.id, "label": p.display_label, "color": p.color,
         "target": p.default_sync_target}
        for p in projects
    ]

    return templates.TemplateResponse(
        "calendar.html",
        _ctx(
            request,
            user,
            columns=columns,
            projects=projects,
            projects_json=projects_json,
            sync_field_registry=sf.registry_json("entry"),
            px_per_hour=CAL_PX_PER_HOUR,
            days_n=days_n,
            m365_connected=m365_conn is not None,
            m365_error=m365_error,
            start=start_d.isoformat(),
            prev_start=(start_d - timedelta(days=days_n)).isoformat(),
            next_start=(start_d + timedelta(days=days_n)).isoformat(),
            today=today.isoformat(),
            range_label=(
                start_d.strftime("%d.%m.%Y")
                if days_n == 1
                else f"{start_d.strftime('%d.%m.')} – {end_d.strftime('%d.%m.%Y')}"
            ),
            error=error,
        ),
    )


@router.post("/calendar/entries")
async def calendar_create(request: Request, db: Session = Depends(get_db)):
    user = _json_user_or_401(request, db)
    if isinstance(user, JSONResponse):
        return user
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Ungültige Anfrage"}, status_code=400)

    try:
        project_id = int(data.get("project_id"))
        entry_date = date.fromisoformat(data.get("entry_date"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Datum oder Projekt fehlt"}, status_code=400)

    project = db.get(Project, project_id)
    if project is None or project.user_id != user.id:
        return JSONResponse({"error": "Projekt nicht gefunden"}, status_code=400)

    start = _parse_time(data.get("start_time"))
    end = _parse_time(data.get("end_time"))
    try:
        duration = _resolve_duration(start, end, None)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    entry = TimeEntry(
        user_id=user.id,
        project_id=project_id,
        entry_date=entry_date,
        start_time=start,
        end_time=end,
        duration_minutes=duration,
        description=(data.get("description") or "").strip(),
    )
    target = project.default_sync_target
    fields = sf.entry_fields(target)
    if fields:
        meta_values = data.get("meta") or {}
        entry.sync_metadata_override, _ = sf.apply_fields(
            entry.sync_metadata_override, target, fields, meta_values
        )
    db.add(entry)
    db.flush()
    es_svc.reconcile_entry_syncs(db, entry, project, load_rules(db))
    db.commit()
    db.refresh(entry)
    return JSONResponse(_cal_entry(entry, project), status_code=201)


@router.post("/calendar/entries/{entry_id}/move")
async def calendar_move(entry_id: int, request: Request, db: Session = Depends(get_db)):
    user = _json_user_or_401(request, db)
    if isinstance(user, JSONResponse):
        return user
    entry = db.get(TimeEntry, entry_id)
    if entry is None or (entry.user_id != user.id and not user.is_admin):
        return JSONResponse({"error": "Eintrag nicht gefunden"}, status_code=404)
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Ungültige Anfrage"}, status_code=400)

    start = _parse_time(data.get("start_time"))
    end = _parse_time(data.get("end_time"))
    try:
        duration = _resolve_duration(start, end, None)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    new_date = _parse_date(data.get("entry_date"))
    if new_date is not None:
        entry.entry_date = new_date
    entry.start_time = start
    entry.end_time = end
    entry.duration_minutes = duration
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return JSONResponse(_cal_entry(entry, db.get(Project, entry.project_id)))

