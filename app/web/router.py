import json
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.models import ImportFormat, Project, TimeEntry, User
from app.schemas.import_format import SUPPORTED_TARGETS
from app.security import create_access_token, hash_password, verify_password
from app.services import app_settings as app_settings_svc
from app.services import reports as report_svc
from app.services import salesforce as sf_svc
from app.services import sync_fields as sf
from app.services.transforms import clean_target_rules, clean_transforms
from app.services.ai_mapping import AiMappingError, suggest_mapping
from app.services.csv_import import import_csv

router = APIRouter(include_in_schema=False)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _maybe_user(request: Request, db: Session) -> User | None:
    token = request.session.get("access_token")
    if not token:
        return None
    try:
        from app.security import decode_token

        payload = decode_token(token)
    except ValueError:
        return None
    return db.get(User, int(payload["sub"]))


_THEMES = {"indigo", "mindsquare", "dark"}


def _ctx(request: Request, user: User | None, **extra) -> dict:
    theme = request.cookies.get("theme")
    if theme not in _THEMES:
        theme = "indigo"
    return {
        "request": request,
        "user": user,
        "theme": theme,
        "ai_enabled": bool(get_settings().anthropic_api_key),
        **extra,
    }


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _group_by_day(entries: list[TimeEntry]) -> list[dict]:
    """Group entries by entry_date (descending) and attach per-day subtotals."""
    grouped: dict[date, list[TimeEntry]] = {}
    for e in entries:
        grouped.setdefault(e.entry_date, []).append(e)
    days = []
    for day in sorted(grouped.keys(), reverse=True):
        items = grouped[day]
        days.append({
            "date": day,
            "entries": items,
            "total_minutes": sum(e.duration_minutes for e in items),
        })
    return days


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    db: Session = Depends(get_db),
    date_from: str | None = None,
    date_to: str | None = None,
    project_id: str | None = None,
    error: str | None = None,
):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    df = _parse_date(date_from)
    dt = _parse_date(date_to)
    project_id_int: int | None = None
    if project_id:
        try:
            project_id_int = int(project_id)
        except ValueError:
            project_id_int = None
    # Default filter window: this month, so the list is bounded but still useful.
    if df is None and dt is None and project_id_int is None:
        today = date.today()
        df = today.replace(day=1)
        dt = today

    stmt = (
        select(TimeEntry)
        .where(TimeEntry.user_id == user.id)
        .order_by(TimeEntry.entry_date.desc(), TimeEntry.id.desc())
    )
    if df is not None:
        stmt = stmt.where(TimeEntry.entry_date >= df)
    if dt is not None:
        stmt = stmt.where(TimeEntry.entry_date <= dt)
    if project_id_int is not None:
        stmt = stmt.where(TimeEntry.project_id == project_id_int)

    entries = list(db.execute(stmt).scalars())
    days = _group_by_day(entries)

    projects = list(
        db.execute(select(Project).where(Project.status == "active").order_by(Project.code)).scalars()
    )
    # All projects (incl. inactive) so we can resolve sync status for every entry.
    proj_lookup = {p.id: p for p in db.execute(select(Project)).scalars()}
    projects_by_id = {p.id: p for p in projects}
    total_minutes = sum(e.duration_minutes for e in entries)

    entry_status = {
        e.id: sf.entry_sync_status(e, proj_lookup[e.project_id])
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
            and e.sync_status != "synced"
        )
        for e in entries
    }
    sf_selectable_count = sum(1 for v in sf_selectable.values() if v)

    formats = _visible_formats(db, user) if entries else []

    return templates.TemplateResponse(
        "dashboard.html",
        _ctx(
            request,
            user,
            days=days,
            projects=projects,
            projects_by_id=projects_by_id,
            entry_status=entry_status,
            sf_selectable=sf_selectable,
            sf_selectable_count=sf_selectable_count,
            sf_configured=sf_configured,
            sync_field_registry=sf.registry_json("entry"),
            project_targets={p.id: p.default_sync_target for p in projects},
            total_hours=round(total_minutes / 60, 2),
            entry_count=len(entries),
            today=date.today().isoformat(),
            date_from=df.isoformat() if df else "",
            date_to=dt.isoformat() if dt else "",
            project_id=project_id_int or "",
            formats=formats,
            error=error,
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
):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

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


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Ungültige Zugangsdaten"},
            status_code=401,
        )
    request.session["access_token"] = create_access_token(user.id)
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)


def _parse_time(value: str | None):
    """Parse an HTML <input type=time> value (HH:MM, sometimes HH:MM:SS)."""
    if not value or not value.strip():
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt).time()
        except ValueError:
            continue
    return None


def _resolve_duration(start, end, duration_minutes: int | None) -> int:
    """start+end win when both present (derive duration); otherwise use the
    explicit duration field. Raises ValueError if neither yields a positive
    duration."""
    if start is not None and end is not None:
        delta = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
        if delta <= 0:
            raise ValueError("Ende muss nach dem Start liegen")
        return delta
    if duration_minutes and duration_minutes > 0:
        return duration_minutes
    raise ValueError("Dauer angeben oder Start + Ende ausfüllen")


@router.post("/entries", response_class=HTMLResponse)
async def create_entry(
    request: Request,
    entry_date: str = Form(...),
    project_id: int = Form(...),
    duration_minutes: int | None = Form(None),
    start_time: str = Form(""),
    end_time: str = Form(""),
    description: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=400, detail="project not found")
    start = _parse_time(start_time)
    end = _parse_time(end_time)
    try:
        duration = _resolve_duration(start, end, duration_minutes)
    except ValueError as e:
        return RedirectResponse(
            url=f"/?error={e}".replace(" ", "+"), status_code=status.HTTP_302_FOUND
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
    # Target-specific fields follow the project's default target on the quick form.
    target = project.default_sync_target
    fields = sf.entry_fields(target)
    if fields:
        form = await request.form()
        values = {f.key: form.get(f"meta__{target}__{f.key}", "") for f in fields}
        entry.sync_metadata_override, _ = sf.apply_fields(
            entry.sync_metadata_override, target, fields, values
        )
    db.add(entry)
    db.commit()
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)


def _owned_entry_or_404(db: Session, entry_id: int, user: User) -> TimeEntry:
    entry = db.get(TimeEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")
    if entry.user_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="not your entry")
    return entry


@router.get("/entries/{entry_id}/edit", response_class=HTMLResponse)
def edit_entry_form(
    request: Request, entry_id: int, db: Session = Depends(get_db), error: str | None = None
):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    entry = _owned_entry_or_404(db, entry_id, user)
    projects = list(
        db.execute(select(Project).order_by(Project.code)).scalars()
    )
    return templates.TemplateResponse(
        "entry_edit.html",
        _ctx(
            request,
            user,
            entry=entry,
            projects=projects,
            sync_targets=_KNOWN_SYNC_TARGETS,
            sync_field_registry=sf.registry_json("entry"),
            project_targets={p.id: p.default_sync_target for p in projects},
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
    duration_minutes: int | None = Form(None),
    start_time: str = Form(""),
    end_time: str = Form(""),
    description: str = Form(""),
    sync_target_override: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    entry = _owned_entry_or_404(db, entry_id, user)
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=400, detail="project not found")
    start = _parse_time(start_time)
    end = _parse_time(end_time)
    try:
        duration = _resolve_duration(start, end, duration_minutes)
    except ValueError as e:
        return RedirectResponse(
            url=f"/entries/{entry_id}/edit?error={e}".replace(" ", "+"),
            status_code=status.HTTP_302_FOUND,
        )
    entry.entry_date = date.fromisoformat(entry_date)
    entry.project_id = project_id
    entry.start_time = start
    entry.end_time = end
    entry.duration_minutes = duration
    entry.description = description

    override = sync_target_override if sync_target_override in _KNOWN_SYNC_TARGETS else ""
    entry.sync_target_override = override or None
    target = override or project.default_sync_target
    fields = sf.entry_fields(target)
    if fields:
        form = await request.form()
        values = {f.key: form.get(f"meta__{target}__{f.key}", "") for f in fields}
        entry.sync_metadata_override, _ = sf.apply_fields(
            entry.sync_metadata_override, target, fields, values
        )
    db.add(entry)
    db.commit()
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)


@router.post("/entries/{entry_id}/delete", response_class=HTMLResponse)
def delete_entry(request: Request, entry_id: int, db: Session = Depends(get_db)):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    entry = _owned_entry_or_404(db, entry_id, user)
    db.delete(entry)
    db.commit()
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)


# -------------------- Calendar (Toggl/Clockify-style) --------------------

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
        "project_label": project.display_label if project else f"#{e.project_id}",
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
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

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
        db.execute(select(Project).where(Project.status == "active").order_by(Project.code)).scalars()
    )
    # All projects (incl. inactive) so sync status resolves for every entry.
    proj_lookup = {p.id: p for p in db.execute(select(Project)).scalars()}

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


def _json_user_or_401(request: Request, db: Session) -> User | JSONResponse:
    user = _maybe_user(request, db)
    if user is None:
        return JSONResponse({"error": "Nicht angemeldet"}, status_code=401)
    return user


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
    if project is None:
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


# -------------------- User management (admin) --------------------


def _require_admin_or_redirect(user: User | None):
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin required")
    return None


@router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    db: Session = Depends(get_db),
    error: str | None = None,
    flash: str | None = None,
):
    user = _maybe_user(request, db)
    redir = _require_admin_or_redirect(user)
    if redir is not None:
        return redir
    users = list(db.execute(select(User).order_by(User.id)).scalars())
    sf_creds = sf_svc.get_credentials(db)
    return templates.TemplateResponse(
        "users.html",
        _ctx(
            request,
            user,
            users=users,
            ai_hints_global=app_settings_svc.get_setting(db, app_settings_svc.AI_HINTS_KEY, ""),
            sf_username=sf_creds["username"],
            sf_login_url=sf_creds["login_url"],
            sf_api_version=sf_creds["api_version"],
            sf_password_set=bool(sf_creds["password"]),
            sf_token_set=bool(sf_creds["security_token"]),
            error=error,
            flash=flash,
        ),
    )


@router.post("/settings/ai-hints", response_class=HTMLResponse)
def settings_ai_hints(request: Request, ai_hints: str = Form(""), db: Session = Depends(get_db)):
    user = _maybe_user(request, db)
    redir = _require_admin_or_redirect(user)
    if redir is not None:
        return redir
    app_settings_svc.set_setting(db, app_settings_svc.AI_HINTS_KEY, ai_hints.strip())
    return RedirectResponse(
        url="/users?flash=Globale+KI-Vorgaben+gespeichert", status_code=status.HTTP_302_FOUND
    )


@router.post("/settings/salesforce", response_class=HTMLResponse)
def settings_salesforce(
    request: Request,
    sf_username: str = Form(""),
    sf_password: str = Form(""),
    sf_security_token: str = Form(""),
    sf_clear_token: bool = Form(False),
    sf_login_url: str = Form(""),
    sf_api_version: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _maybe_user(request, db)
    redir = _require_admin_or_redirect(user)
    if redir is not None:
        return redir
    sf_svc.save_credentials(
        db,
        username=sf_username,
        password=sf_password,
        security_token=sf_security_token,
        clear_security_token=sf_clear_token,
        login_url=sf_login_url,
        api_version=sf_api_version,
    )
    return RedirectResponse(
        url="/users?flash=Salesforce-Zugangsdaten+gespeichert",
        status_code=status.HTTP_302_FOUND,
    )


@router.get("/sync", response_class=HTMLResponse)
def sync_center(request: Request, db: Session = Depends(get_db)):
    """Hub for sync actions. Lists per-target counts and a 'preview ready
    entries' button per implemented target, plus the CSV-Export."""
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    entries = list(
        db.execute(select(TimeEntry).where(TimeEntry.user_id == user.id)).scalars()
    )
    proj_lookup = {p.id: p for p in db.execute(select(Project)).scalars()}

    targets = ("jira", "salesforce", "bcs")
    counts: dict[str, dict] = {t: {"ready_ids": [], "pending": 0, "synced": 0} for t in targets}
    for e in entries:
        project = proj_lookup.get(e.project_id)
        if project is None:
            continue
        st = sf.entry_sync_status(e, project)
        t = st["target"]
        if t not in counts:
            continue
        if e.sync_status == "synced":
            counts[t]["synced"] += 1
        elif st["ready"]:
            counts[t]["ready_ids"].append(e.id)
        else:
            counts[t]["pending"] += 1

    formats = _visible_formats(db, user)
    return templates.TemplateResponse(
        "sync_center.html",
        _ctx(
            request,
            user,
            counts=counts,
            sf_configured=sf_svc.credentials_configured(db),
            formats=formats,
        ),
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
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
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
    proj_lookup = {p.id: p for p in db.execute(select(Project)).scalars()}

    client = sf_svc.client_from_settings(db)
    if client is None:
        return templates.TemplateResponse(
            "sync_salesforce_preview.html",
            _ctx(request, user, error=(
                "Salesforce-Zugangsdaten sind nicht hinterlegt. "
                "Admin: unter Nutzer → Salesforce-Integration eintragen."
            ), groups=[], errors=[], entries=entries),
        )

    # Step 1: gather assignment IDs and resolve each (one SOQL per id).
    assignment_ids: list[str] = []
    per_entry_assignment: dict[int, str | None] = {}
    item_errors: list[dict] = []
    for e in entries:
        project = proj_lookup.get(e.project_id)
        aid = sf_svc.assignment_id_for(e, project) if project else None
        per_entry_assignment[e.id] = aid
        if aid and aid not in assignment_ids:
            assignment_ids.append(aid)

    assignments: dict[str, dict] = {}
    sf_error: str | None = None
    try:
        for aid in assignment_ids:
            a = sf_svc.get_assignment(client, aid)
            if a is None:
                item_errors.append({"assignment_id": aid,
                                    "error": "Projektbesetzung in Salesforce nicht gefunden"})
                continue
            assignments[aid] = a
    except sf_svc.SalesforceError as e:
        sf_error = str(e)

    # Step 2: pro Eintrag den Kontierungsmonat suchen (PB-spezifisch!) und
    # gruppieren nach (Projektbesetzung × Kontierungsmonat).
    grouped: dict[tuple[str, str], dict] = {}
    skipped: list[dict] = []
    period_cache: dict[tuple[str, str], dict | None] = {}  # (aid, YYYY-MM) → period or None
    if sf_error is None:
        for e in entries:
            aid = per_entry_assignment[e.id]
            if not aid:
                skipped.append({"entry": e, "reason": "keine Projektbesetzung gepflegt"})
                continue
            if aid not in assignments:
                skipped.append({"entry": e, "reason": "Projektbesetzung nicht in SF gefunden"})
                continue
            assignment = assignments[aid]
            if assignment.get("closed"):
                skipped.append({"entry": e, "reason": "Projektbesetzung in SF geschlossen"})
                continue

            cache_key = (aid, e.entry_date.strftime("%Y-%m"))
            if cache_key not in period_cache:
                try:
                    period_cache[cache_key] = sf_svc.get_monthly_period(
                        client, aid, e.entry_date.isoformat()
                    )
                except sf_svc.SalesforceError as err:
                    sf_error = str(err)
                    break
            period = period_cache[cache_key]
            if period is None:
                skipped.append({
                    "entry": e,
                    "reason": f"Kein Kontierungsmonat {e.entry_date.strftime('%m/%Y')} "
                              f"für diese Projektbesetzung in SF",
                })
                continue
            if period.get("closed"):
                skipped.append({
                    "entry": e,
                    "reason": f"Kontierungsmonat {period.get('name') or e.entry_date.strftime('%m/%Y')} "
                              f"ist abgeschlossen",
                })
                continue

            project = proj_lookup.get(e.project_id)
            remote_value = ((e.sync_metadata_override or {}).get("salesforce") or {}).get("remote")
            if not remote_value and project is not None:
                remote_value = ((project.sync_metadata or {}).get("salesforce") or {}).get("remote")
            payload = sf_svc.build_zeiterfassung_payload(e, period["id"], remote_value)

            group = grouped.setdefault((aid, period["id"]), {
                "assignment": assignment,
                "period": period,
                "entries": [],
                "total_hours": 0.0,
            })
            group["entries"].append({
                "entry": e,
                "payload": payload,
                "remote_value": remote_value or "",
            })
            group["total_hours"] = round(group["total_hours"] + e.duration_minutes / 60.0, 2)

    groups = list(grouped.values())

    return templates.TemplateResponse(
        "sync_salesforce_preview.html",
        _ctx(
            request,
            user,
            groups=groups,
            skipped=skipped,
            item_errors=item_errors,
            sf_error=sf_error,
            entries=entries,
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
    user = _maybe_user(request, db)
    redir = _require_admin_or_redirect(user)
    if redir is not None:
        return redir
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


@router.post("/settings/salesforce/test", response_class=HTMLResponse)
def settings_salesforce_test(request: Request, db: Session = Depends(get_db)):
    """Try a SOAP login against the stored credentials and report the result."""
    user = _maybe_user(request, db)
    redir = _require_admin_or_redirect(user)
    if redir is not None:
        return redir
    client = sf_svc.client_from_settings(db)
    if client is None:
        return RedirectResponse(
            url="/users?error=Bitte+Username+und+Passwort+hinterlegen",
            status_code=status.HTTP_302_FOUND,
        )
    try:
        client.login()
    except sf_svc.SalesforceError as e:
        from urllib.parse import quote_plus
        return RedirectResponse(
            url=f"/users?error=Salesforce-Login+fehlgeschlagen:+{quote_plus(str(e))[:200]}",
            status_code=status.HTTP_302_FOUND,
        )
    return RedirectResponse(
        url=f"/users?flash=Salesforce-Login+ok+%28{client.instance_url}%29",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/users", response_class=HTMLResponse)
def users_create(
    request: Request,
    email: str = Form(...),
    full_name: str = Form(""),
    password: str = Form(...),
    is_admin: bool = Form(False),
    db: Session = Depends(get_db),
):
    user = _maybe_user(request, db)
    redir = _require_admin_or_redirect(user)
    if redir is not None:
        return redir
    new = User(
        email=email,
        full_name=full_name,
        hashed_password=hash_password(password),
        is_admin=is_admin,
        is_active=True,
    )
    db.add(new)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url="/users?error=" + "E-Mail+bereits+vergeben", status_code=status.HTTP_302_FOUND
        )
    return RedirectResponse(url="/users", status_code=status.HTTP_302_FOUND)


@router.post("/users/{user_id}/toggle-active", response_class=HTMLResponse)
def users_toggle_active(request: Request, user_id: int, db: Session = Depends(get_db)):
    actor = _maybe_user(request, db)
    redir = _require_admin_or_redirect(actor)
    if redir is not None:
        return redir
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Not found")
    if target.id == actor.id:
        return RedirectResponse(
            url="/users?error=Eigenen+Account+nicht+deaktivieren",
            status_code=status.HTTP_302_FOUND,
        )
    target.is_active = not target.is_active
    db.add(target)
    db.commit()
    return RedirectResponse(url="/users", status_code=status.HTTP_302_FOUND)


@router.post("/users/{user_id}/toggle-admin", response_class=HTMLResponse)
def users_toggle_admin(request: Request, user_id: int, db: Session = Depends(get_db)):
    actor = _maybe_user(request, db)
    redir = _require_admin_or_redirect(actor)
    if redir is not None:
        return redir
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Not found")
    if target.id == actor.id:
        return RedirectResponse(
            url="/users?error=Eigene+Adminrechte+nicht+entziehen",
            status_code=status.HTTP_302_FOUND,
        )
    target.is_admin = not target.is_admin
    db.add(target)
    db.commit()
    return RedirectResponse(url="/users", status_code=status.HTTP_302_FOUND)


# -------------------- Import formats --------------------


def _visible_formats(db: Session, user: User) -> list[ImportFormat]:
    stmt = (
        select(ImportFormat)
        .where(or_(ImportFormat.is_global.is_(True), ImportFormat.owner_id == user.id))
        .order_by(ImportFormat.is_global.desc(), ImportFormat.name)
    )
    return list(db.execute(stmt).scalars())


# Friendlier labels for a few plain targets (the duration trio especially).
_TARGET_LABELS = {
    "entry_date": "Datum",
    "start_time": "Startzeit",
    "end_time": "Endzeit",
    "duration": "Dauer (automatisch)",
    "duration_minutes": "Dauer in Minuten",
    "duration_hours": "Dauer in Stunden",
    "project_code": "Projekt (Code/Name)",
    "customer": "Kunde",
    "description": "Beschreibung",
    "tags": "Tags",
    "sync_target": "Sync-Ziel (pro Zeile)",
    "external_ref": "Externe Referenz",
}

# Order of the always-shown standard target rows (duration is injected after the
# time fields by the template).
_STANDARD_ROW_ORDER = [
    "entry_date", "start_time", "end_time",
    "project_code", "customer", "description", "tags", "sync_target", "external_ref",
]


def _target_label(token: str) -> str:
    """Human label for any mapping target (plain or sync), for previews/UI."""
    if token.startswith("sync:"):
        return sf.target_label(token)
    return _TARGET_LABELS.get(token, token)


def _mapping_rows() -> dict:
    """Structured target rows for the target-oriented mapping editor."""
    standard = [{"value": t, "label": _target_label(t)} for t in _STANDARD_ROW_ORDER]
    sync = [
        {"value": t, "label": sf.target_label(t)}
        for t in sorted(SUPPORTED_TARGETS) if t.startswith("sync:")
    ]
    return {"standard": standard, "sync": sync}


def _parse_column_map(raw: str) -> dict:
    """Parse the target-keyed column_map JSON ({target: source}), keeping only
    known targets with a non-empty source."""
    try:
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if k in SUPPORTED_TARGETS and v}


def _invert_map(d: dict) -> dict:
    """Swap keys/values (target<->source). Used at the AI boundary, which speaks
    source->target while TimeHub stores target->source."""
    return {v: k for k, v in d.items()}


def _target_options() -> list[dict]:
    """Flat list of all mapping targets with labels (used for the 'supported
    fields' hint on the upload screen)."""
    base = sorted(t for t in SUPPORTED_TARGETS if not t.startswith("sync:"))
    sync = sorted(t for t in SUPPORTED_TARGETS if t.startswith("sync:"))
    return [{"value": t, "label": _target_label(t)} for t in base + sync]


def _parse_transforms(raw: str) -> list[dict]:
    """Parse the transforms_json hidden field into a clean list of rules.
    Invalid entries are dropped rather than failing the whole save."""
    try:
        data = json.loads(raw) if raw.strip() else []
    except (json.JSONDecodeError, ValueError):
        return []
    return clean_transforms(data, SUPPORTED_TARGETS)


def _parse_target_rules(raw: str) -> list[dict]:
    try:
        data = json.loads(raw) if raw.strip() else []
    except (json.JSONDecodeError, ValueError):
        return []
    return clean_target_rules(data, set(_KNOWN_SYNC_TARGETS))


def _ai_hints(db: Session, user: User | None) -> str:
    """Combine global (admin) and personal standing instructions for the AI."""
    parts = []
    g = app_settings_svc.get_setting(db, app_settings_svc.AI_HINTS_KEY, "")
    if g and g.strip():
        parts.append(g.strip())
    if user and user.ai_hints and user.ai_hints.strip():
        parts.append(user.ai_hints.strip())
    return "\n".join(parts)


# Keep the stored sample small — same budget as what we send to the AI.
_SAMPLE_MAX_LINES = 30


def _trim_sample(text: str) -> str:
    lines = text.splitlines()
    return "\n".join(lines[:_SAMPLE_MAX_LINES])[:8000]


def _peek_headers(sample: str, separator: str) -> list[str]:
    import csv as _csv
    import io as _io
    if not sample.strip():
        return []
    reader = _csv.reader(_io.StringIO(sample), delimiter=separator)
    try:
        return [h.lstrip("﻿").strip() for h in next(reader)]
    except StopIteration:
        return []


def _headers_union(sample: str, separator: str, column_map: dict) -> list[str]:
    """Every source column from the stored sample, plus any mapped source not in
    it — so ignored columns stay available as mapping/transform sources.
    column_map is target-keyed, so the sources are its values."""
    headers = _peek_headers(sample, separator)
    seen = set(headers)
    for src in column_map.values():
        if src and src not in seen:
            headers.append(src)
            seen.add(src)
    return headers


@router.get("/import-formats", response_class=HTMLResponse)
def formats_list(
    request: Request,
    db: Session = Depends(get_db),
    flash: str | None = None,
    error: str | None = None,
):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    formats = _visible_formats(db, user)
    return templates.TemplateResponse(
        "import_formats.html",
        _ctx(request, user, formats=formats, flash=flash, error=error),
    )


@router.get("/import-formats/new", response_class=HTMLResponse)
def formats_new_form(request: Request, db: Session = Depends(get_db)):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(
        "import_format_new.html",
        _ctx(request, user, error=None, target_options=_target_options()),
    )


@router.post("/import-formats/new", response_class=HTMLResponse)
async def formats_new_submit(
    request: Request,
    name: str = Form(...),
    sample: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    raw = await sample.read()
    text = raw.decode("utf-8", errors="replace")
    try:
        suggestion = suggest_mapping(text, hints=_ai_hints(db, user))
    except AiMappingError as e:
        return templates.TemplateResponse(
            "import_format_new.html",
            _ctx(
                request,
                user,
                error=str(e),
                target_options=_target_options(),
                prefill_name=name,
            ),
            status_code=400,
        )

    sep = suggestion.separator if suggestion.separator != "\\t" else "\t"
    source_rows, target_rows = report_svc.preview_via_import_format(
        text,
        suggestion.column_map,
        separator=sep,
        date_format=suggestion.date_format,
        time_format=suggestion.time_format,
        transforms=suggestion.transforms,
    )
    return templates.TemplateResponse(
        "import_format_review.html",
        _ctx(
            request,
            user,
            name=name,
            suggestion=suggestion,
            source_rows=source_rows,
            target_rows=target_rows,
            target_fields=sorted(SUPPORTED_TARGETS),
            target_options=_target_options(),
            mapping_rows=_mapping_rows(),
            mapping=suggestion.column_map,
            headers=suggestion.detected_headers,
            transforms=suggestion.transforms,
            target_rules=suggestion.target_rules,
            sample_text=_trim_sample(text),
            tlabel=_target_label,
            error=None,
        ),
    )


@router.post("/import-formats/refine", response_class=HTMLResponse)
def formats_refine(
    request: Request,
    name: str = Form(...),
    sample_text: str = Form(""),
    instruction: str = Form(""),
    source_hint: str = Form("custom"),
    separator: str = Form(","),
    encoding: str = Form("utf-8"),
    date_format: str = Form("%Y-%m-%d"),
    time_format: str = Form("%H:%M"),
    default_project_code: str = Form(""),
    notes: str = Form(""),
    column_map_json: str = Form("{}"),
    transforms_json: str = Form("[]"),
    target_rules_json: str = Form("[]"),
    db: Session = Depends(get_db),
):
    """Refinement turn for the format wizard: re-run the AI with the current
    state as 'previous' plus the user's instruction, and re-render the review."""
    from app.schemas.import_format import ImportFormatSuggestion

    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    canonical_map = _parse_column_map(column_map_json)  # target-keyed

    previous = {
        "source_hint": source_hint or "custom",
        "separator": separator or ",",
        "encoding": encoding or "utf-8",
        "date_format": date_format or "%Y-%m-%d",
        "time_format": time_format or "%H:%M",
        # the model speaks source->target
        "column_map": _invert_map(canonical_map),
        "transforms": _parse_transforms(transforms_json),
        "target_rules": _parse_target_rules(target_rules_json),
        "default_project_code": default_project_code.strip() or None,
        "notes": notes,
    }

    def _from_previous() -> "ImportFormatSuggestion":
        return ImportFormatSuggestion(
            source_hint=previous["source_hint"],
            separator=previous["separator"],
            encoding=previous["encoding"],
            date_format=previous["date_format"],
            time_format=previous["time_format"],
            column_map=canonical_map,
            transforms=previous["transforms"],
            target_rules=previous["target_rules"],
            default_project_code=previous["default_project_code"],
            notes=previous["notes"],
            detected_headers=list(canonical_map.values()),
        )

    error = None
    if not instruction.strip():
        suggestion = _from_previous()
        error = "Bitte eine Anweisung eingeben, was die KI anpassen soll."
    else:
        try:
            suggestion = suggest_mapping(
                sample_text, instruction=instruction, previous=previous, hints=_ai_hints(db, user)
            )
        except AiMappingError as e:
            suggestion = _from_previous()
            error = str(e)

    sep = suggestion.separator if suggestion.separator != "\\t" else "\t"
    source_rows, target_rows = report_svc.preview_via_import_format(
        sample_text,
        suggestion.column_map,
        separator=sep,
        date_format=suggestion.date_format,
        time_format=suggestion.time_format,
        transforms=suggestion.transforms,
    )
    # Show every sample column (plus any source the suggestion references) in the UI.
    if source_rows:
        headers = list(source_rows[0].keys())
        for src in suggestion.column_map.values():
            if src and src not in headers:
                headers.append(src)
        suggestion.detected_headers = headers

    return templates.TemplateResponse(
        "import_format_review.html",
        _ctx(
            request,
            user,
            name=name,
            suggestion=suggestion,
            source_rows=source_rows,
            target_rows=target_rows,
            target_fields=sorted(SUPPORTED_TARGETS),
            mapping_rows=_mapping_rows(),
            mapping=suggestion.column_map,
            target_options=_target_options(),
            headers=suggestion.detected_headers,
            transforms=suggestion.transforms,
            target_rules=suggestion.target_rules,
            sample_text=sample_text,
            tlabel=_target_label,
            error=error,
        ),
    )


@router.post("/import-formats/preview", response_class=HTMLResponse)
def formats_preview(
    request: Request,
    sample_text: str = Form(""),
    separator: str = Form(","),
    date_format: str = Form("%Y-%m-%d"),
    time_format: str = Form("%H:%M"),
    column_map_json: str = Form("{}"),
    transforms_json: str = Form("[]"),
    db: Session = Depends(get_db),
):
    """Live preview fragment: render the current mapping + transforms against
    the sample. Returned as an HTML partial the editor swaps in on change."""
    user = _maybe_user(request, db)
    if user is None:
        return HTMLResponse("", status_code=401)
    column_map = _parse_column_map(column_map_json)
    transforms = _parse_transforms(transforms_json)
    sep = separator if separator and separator != "\\t" else (separator or ",")
    source_rows, target_rows = report_svc.preview_via_import_format(
        sample_text,
        column_map,
        separator=sep,
        date_format=date_format or "%Y-%m-%d",
        time_format=time_format or "%H:%M",
        transforms=transforms,
    )
    return templates.TemplateResponse(
        "_preview_panel.html",
        {
            "request": request,
            "source_rows": source_rows,
            "target_rows": target_rows,
            "target_fields": sorted(SUPPORTED_TARGETS),
            "tlabel": sf.target_label,
        },
    )


@router.post("/import-formats", response_class=HTMLResponse)
async def formats_save(
    request: Request,
    name: str = Form(...),
    source_hint: str = Form("custom"),
    separator: str = Form(","),
    encoding: str = Form("utf-8"),
    date_format: str = Form("%Y-%m-%d"),
    time_format: str = Form("%H:%M"),
    default_project_code: str = Form(""),
    notes: str = Form(""),
    column_map_json: str = Form("{}"),
    transforms_json: str = Form("[]"),
    target_rules_json: str = Form("[]"),
    sample_text: str = Form(""),
    is_global: bool = Form(False),
    db: Session = Depends(get_db),
):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    column_map = _parse_column_map(column_map_json)

    fmt = ImportFormat(
        name=name,
        source_hint=source_hint or "custom",
        separator=separator or ",",
        encoding=encoding or "utf-8",
        date_format=date_format or "%Y-%m-%d",
        time_format=time_format or "%H:%M",
        column_map=column_map,
        transforms=_parse_transforms(transforms_json),
        target_rules=_parse_target_rules(target_rules_json),
        sample_data=(_trim_sample(sample_text) or None),
        default_project_code=(default_project_code.strip() or None),
        notes=notes,
        owner_id=user.id,
        is_global=(is_global and user.is_admin),
    )
    db.add(fmt)
    db.commit()
    return RedirectResponse(
        url=f"/import-formats?flash=Format+'{name}'+gespeichert",
        status_code=status.HTTP_302_FOUND,
    )


@router.get("/import-formats/{fmt_id}/edit", response_class=HTMLResponse)
def formats_edit_form(request: Request, fmt_id: int, db: Session = Depends(get_db)):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    fmt = db.get(ImportFormat, fmt_id)
    if fmt is None or (not fmt.is_global and fmt.owner_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="format not found")
    # writable check — same rule as the delete handler so we don't render an
    # edit form the user can't actually submit.
    if fmt.owner_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="not allowed to edit this format")
    sample = fmt.sample_data or ""
    sep = fmt.separator if fmt.separator != "\\t" else "\t"
    source_rows, target_rows = report_svc.preview_via_import_format(
        sample, fmt.column_map, separator=sep,
        date_format=fmt.date_format, time_format=fmt.time_format,
        transforms=fmt.transforms or [],
    )
    return templates.TemplateResponse(
        "import_format_edit.html",
        _ctx(
            request,
            user,
            fmt=fmt,
            target_options=_target_options(),
            mapping_rows=_mapping_rows(),
            mapping=fmt.column_map,
            column_map=fmt.column_map,
            transforms=fmt.transforms or [],
            target_rules=fmt.target_rules or [],
            # All columns from the stored sample stay available — even ignored
            # ones — plus any mapped header not in the sample.
            headers=_headers_union(sample, fmt.separator, fmt.column_map),
            sample_text=sample,
            source_rows=source_rows,
            target_rows=target_rows,
            target_fields=sorted(SUPPORTED_TARGETS),
            tlabel=_target_label,
            error=None,
        ),
    )


@router.post("/import-formats/{fmt_id}/edit", response_class=HTMLResponse)
async def formats_edit_submit(
    request: Request,
    fmt_id: int,
    name: str = Form(...),
    source_hint: str = Form("custom"),
    separator: str = Form(","),
    encoding: str = Form("utf-8"),
    date_format: str = Form("%Y-%m-%d"),
    time_format: str = Form("%H:%M"),
    default_project_code: str = Form(""),
    notes: str = Form(""),
    column_map_json: str = Form("{}"),
    transforms_json: str = Form("[]"),
    target_rules_json: str = Form("[]"),
    sample_text: str = Form(""),
    is_global: bool = Form(False),
    db: Session = Depends(get_db),
):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    fmt = db.get(ImportFormat, fmt_id)
    if fmt is None:
        raise HTTPException(status_code=404, detail="format not found")
    if fmt.owner_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="not allowed")

    column_map = _parse_column_map(column_map_json)

    fmt.name = name
    fmt.source_hint = source_hint or "custom"
    fmt.separator = separator or ","
    fmt.encoding = encoding or "utf-8"
    fmt.date_format = date_format or "%Y-%m-%d"
    fmt.time_format = time_format or "%H:%M"
    fmt.column_map = column_map
    fmt.transforms = _parse_transforms(transforms_json)
    fmt.target_rules = _parse_target_rules(target_rules_json)
    fmt.sample_data = _trim_sample(sample_text) or None
    fmt.default_project_code = default_project_code.strip() or None
    fmt.notes = notes
    # only admins may flip the global flag
    if user.is_admin:
        fmt.is_global = bool(is_global)
    db.add(fmt)
    db.commit()
    return RedirectResponse(
        url=f"/import-formats?flash=Format+'{name}'+aktualisiert",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/import-formats/{fmt_id}/refine", response_class=HTMLResponse)
def formats_edit_refine(
    request: Request,
    fmt_id: int,
    name: str = Form(...),
    sample_text: str = Form(""),
    instruction: str = Form(""),
    source_hint: str = Form("custom"),
    separator: str = Form(","),
    encoding: str = Form("utf-8"),
    date_format: str = Form("%Y-%m-%d"),
    time_format: str = Form("%H:%M"),
    default_project_code: str = Form(""),
    notes: str = Form(""),
    column_map_json: str = Form("{}"),
    transforms_json: str = Form("[]"),
    target_rules_json: str = Form("[]"),
    db: Session = Depends(get_db),
):
    """AI refinement that stays on the edit screen: re-runs the model with the
    current state + instruction, then re-renders the edit form (unsaved) so the
    user can review and Save."""
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    fmt = db.get(ImportFormat, fmt_id)
    if fmt is None:
        raise HTTPException(status_code=404, detail="format not found")
    if fmt.owner_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="not allowed")

    column_map = _parse_column_map(column_map_json)  # target-keyed
    transforms = _parse_transforms(transforms_json)
    target_rules = _parse_target_rules(target_rules_json)

    error = None
    if not instruction.strip():
        error = "Bitte eine Anweisung eingeben, was die KI anpassen soll."
    elif not sample_text.strip():
        error = "Für die KI werden Beispieldaten benötigt — bitte unten einfügen."
    else:
        previous = {
            "source_hint": source_hint, "separator": separator, "encoding": encoding,
            "date_format": date_format, "time_format": time_format,
            "column_map": _invert_map(column_map),  # the model speaks source->target
            "transforms": transforms, "target_rules": target_rules,
            "default_project_code": default_project_code.strip() or None, "notes": notes,
        }
        try:
            suggestion = suggest_mapping(
                sample_text, instruction=instruction, previous=previous, hints=_ai_hints(db, user)
            )
            source_hint = suggestion.source_hint
            separator = suggestion.separator
            encoding = suggestion.encoding
            date_format = suggestion.date_format
            time_format = suggestion.time_format
            column_map = suggestion.column_map
            transforms = suggestion.transforms
            target_rules = suggestion.target_rules
            notes = suggestion.notes or notes
        except AiMappingError as e:
            error = str(e)

    # Reflect the (unsaved) current/refined values in the re-render. The session
    # never commits this — we expunge so an accidental flush can't persist it.
    fmt.name = name
    fmt.source_hint = source_hint
    fmt.separator = separator
    fmt.encoding = encoding
    fmt.date_format = date_format
    fmt.time_format = time_format
    fmt.default_project_code = default_project_code.strip() or None
    fmt.notes = notes
    db.expunge(fmt)
    sep = separator if separator != "\\t" else "\t"
    source_rows, target_rows = report_svc.preview_via_import_format(
        sample_text, column_map, separator=sep,
        date_format=date_format, time_format=time_format, transforms=transforms,
    )
    return templates.TemplateResponse(
        "import_format_edit.html",
        _ctx(
            request,
            user,
            fmt=fmt,
            target_options=_target_options(),
            mapping_rows=_mapping_rows(),
            mapping=column_map,
            column_map=column_map,
            transforms=transforms,
            target_rules=target_rules,
            headers=_headers_union(sample_text, separator, column_map),
            sample_text=sample_text,
            source_rows=source_rows,
            target_rows=target_rows,
            target_fields=sorted(SUPPORTED_TARGETS),
            tlabel=_target_label,
            error=error,
        ),
    )


@router.post("/import-formats/{fmt_id}/delete", response_class=HTMLResponse)
def formats_delete(request: Request, fmt_id: int, db: Session = Depends(get_db)):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    fmt = db.get(ImportFormat, fmt_id)
    if fmt is None:
        raise HTTPException(status_code=404, detail="Not found")
    if fmt.owner_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="not allowed")
    db.delete(fmt)
    db.commit()
    return RedirectResponse(url="/import-formats", status_code=status.HTTP_302_FOUND)


@router.post("/import-formats/{fmt_id}/promote", response_class=HTMLResponse)
def formats_promote(request: Request, fmt_id: int, db: Session = Depends(get_db)):
    user = _maybe_user(request, db)
    redir = _require_admin_or_redirect(user)
    if redir is not None:
        return redir
    fmt = db.get(ImportFormat, fmt_id)
    if fmt is None:
        raise HTTPException(status_code=404, detail="Not found")
    fmt.is_global = not fmt.is_global
    db.add(fmt)
    db.commit()
    return RedirectResponse(url="/import-formats", status_code=status.HTTP_302_FOUND)


@router.get("/import", response_class=HTMLResponse)
def import_form(
    request: Request,
    db: Session = Depends(get_db),
    result: str | None = None,
    error: str | None = None,
):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    formats = _visible_formats(db, user)
    return templates.TemplateResponse(
        "import_run.html",
        _ctx(request, user, formats=formats, result=result, error=error),
    )


@router.post("/import", response_class=HTMLResponse)
async def import_run(
    request: Request,
    format_id: int = Form(...),
    file: UploadFile = File(...),
    apply_target_rules: bool = Form(False),
    db: Session = Depends(get_db),
):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    fmt = db.get(ImportFormat, format_id)
    if fmt is None or (not fmt.is_global and fmt.owner_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="format not found")
    raw = await file.read()
    try:
        result = import_csv(
            db,
            user_id=user.id,
            raw_bytes=raw,
            column_map=fmt.column_map,
            default_project_code=fmt.default_project_code,
            separator=fmt.separator,
            encoding=fmt.encoding,
            date_format=fmt.date_format,
            time_format=fmt.time_format,
            transforms=fmt.transforms or [],
            target_rules=fmt.target_rules or [],
            apply_target_rules=apply_target_rules,
        )
    except ValueError as e:
        formats = _visible_formats(db, user)
        return templates.TemplateResponse(
            "import_run.html",
            _ctx(request, user, formats=formats, result=None, error=str(e), fmt=fmt),
            status_code=400,
        )

    # Remember the uploaded CSV as the format's sample (only if none stored yet),
    # so preview & AI keep working on the edit screen without a re-upload.
    if not fmt.sample_data:
        fmt.sample_data = _trim_sample(raw.decode("utf-8", errors="replace"))
        db.add(fmt)
        db.commit()

    formats = _visible_formats(db, user)
    return templates.TemplateResponse(
        "import_run.html",
        _ctx(request, user, formats=formats, result=result, error=None, fmt=fmt),
    )


# -------------------- Projects --------------------


_KNOWN_SYNC_TARGETS = ["intern", "jira", "salesforce", "bcs", "none"]
_KNOWN_STATUSES = ["active", "inactive"]


def _unique_project_code(db: Session, name: str) -> str:
    """Derive a stable, unique code from the project name so users only have to
    enter a name. Normalizes the same way the importer matches codes, so an
    auto-code stays compatible with importing the plain name."""
    import re as _re

    base = _re.sub(r"[^A-Za-z0-9]+", "-", (name or "").strip()).strip("-").upper()[:60] or "PROJEKT"
    existing = {c for (c,) in db.execute(select(Project.code)).all()}
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
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    projects = list(db.execute(select(Project).order_by(Project.code)).scalars())
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
    user = _maybe_user(request, db)
    redir = _require_admin_or_redirect(user)
    if redir is not None:
        return redir
    target = default_sync_target if default_sync_target in _KNOWN_SYNC_TARGETS else "intern"
    # Code is optional — derive a unique one from the name when left blank.
    final_code = code.strip() or _unique_project_code(db, name)
    p = Project(
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
    user = _maybe_user(request, db)
    redir = _require_admin_or_redirect(user)
    if redir is not None:
        return redir
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Not found")
    return templates.TemplateResponse(
        "project_edit.html",
        _ctx(
            request,
            user,
            project=project,
            sync_targets=_KNOWN_SYNC_TARGETS,
            sync_field_registry=sf.registry_json("project"),
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
    user = _maybe_user(request, db)
    redir = _require_admin_or_redirect(user)
    if redir is not None:
        return redir
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Not found")
    project.name = name.strip()
    # Code optional: keep the existing one when the field is left blank.
    project.code = code.strip() or project.code or _unique_project_code(db, name)
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
    user = _maybe_user(request, db)
    redir = _require_admin_or_redirect(user)
    if redir is not None:
        return redir
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Not found")
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


# -------------------- Settings & profile --------------------


@router.post("/settings/theme")
def set_theme(request: Request, theme: str = Form(...)):
    if theme not in _THEMES:
        theme = "indigo"
    referer = request.headers.get("referer") or "/"
    resp = RedirectResponse(url=referer, status_code=status.HTTP_302_FOUND)
    # 1 year cookie; SameSite=lax so it survives the inline form POST.
    resp.set_cookie("theme", theme, max_age=365 * 24 * 3600, samesite="lax")
    return resp


@router.get("/profile", response_class=HTMLResponse)
def profile_page(
    request: Request,
    db: Session = Depends(get_db),
    flash: str | None = None,
    error: str | None = None,
):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(
        "profile.html",
        _ctx(request, user, flash=flash, error=error),
    )


@router.post("/profile", response_class=HTMLResponse)
def profile_save(
    request: Request,
    full_name: str = Form(""),
    ai_hints: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    user.full_name = full_name.strip()
    user.ai_hints = ai_hints.strip() or None
    db.add(user)
    db.commit()
    return RedirectResponse(
        url="/profile?flash=Profil+gespeichert", status_code=status.HTTP_302_FOUND
    )


# -------------------- Reporting --------------------


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
    user_id: str | None = None,
):
    from app.services import report_builder as rb

    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

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
    uid: int | None = None
    if user_id and user.is_admin:
        try:
            uid = int(user_id)
        except ValueError:
            uid = None

    stmt = (
        select(TimeEntry, Project, User)
        .join(Project, Project.id == TimeEntry.project_id)
        .join(User, User.id == TimeEntry.user_id)
        .order_by(TimeEntry.entry_date, TimeEntry.id)
    )
    # Non-admins only ever see their own entries.
    if not user.is_admin:
        stmt = stmt.where(TimeEntry.user_id == user.id)
    elif uid is not None:
        stmt = stmt.where(TimeEntry.user_id == uid)
    if df is not None:
        stmt = stmt.where(TimeEntry.entry_date >= df)
    if dt is not None:
        stmt = stmt.where(TimeEntry.entry_date <= dt)
    if pid is not None:
        stmt = stmt.where(TimeEntry.project_id == pid)
    if customer:
        stmt = stmt.where(Project.customer == customer)

    rows = list(db.execute(stmt).all())
    report = rb.build_report(rows, active_group_by, detailed=active_detailed)

    projects = list(db.execute(select(Project).order_by(Project.code)).scalars())
    customers = sorted({p.customer for p in projects if p.customer})
    users = (
        list(db.execute(select(User).order_by(User.full_name)).scalars())
        if user.is_admin
        else []
    )

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
            users=users,
            date_from=df.isoformat() if df else "",
            date_to=dt.isoformat() if dt else "",
            project_id=pid or "",
            customer=customer or "",
            user_id=uid or "",
        ),
    )
