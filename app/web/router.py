import json
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
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
from app.services import reports as report_svc
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
    projects_by_id = {p.id: p for p in projects}
    total_minutes = sum(e.duration_minutes for e in entries)

    formats = _visible_formats(db, user) if entries else []

    return templates.TemplateResponse(
        "dashboard.html",
        _ctx(
            request,
            user,
            days=days,
            projects=projects,
            projects_by_id=projects_by_id,
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
def create_entry(
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
        _ctx(request, user, entry=entry, projects=projects, error=error),
    )


@router.post("/entries/{entry_id}/edit", response_class=HTMLResponse)
def edit_entry_submit(
    request: Request,
    entry_id: int,
    entry_date: str = Form(...),
    project_id: int = Form(...),
    duration_minutes: int | None = Form(None),
    start_time: str = Form(""),
    end_time: str = Form(""),
    description: str = Form(""),
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


# -------------------- User management (admin) --------------------


def _require_admin_or_redirect(user: User | None):
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin required")
    return None


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_db), error: str | None = None):
    user = _maybe_user(request, db)
    redir = _require_admin_or_redirect(user)
    if redir is not None:
        return redir
    users = list(db.execute(select(User).order_by(User.id)).scalars())
    return templates.TemplateResponse(
        "users.html", _ctx(request, user, users=users, error=error)
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
        _ctx(request, user, error=None, targets=sorted(SUPPORTED_TARGETS)),
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
        suggestion = suggest_mapping(text)
    except AiMappingError as e:
        return templates.TemplateResponse(
            "import_format_new.html",
            _ctx(
                request,
                user,
                error=str(e),
                targets=sorted(SUPPORTED_TARGETS),
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
            targets=sorted(SUPPORTED_TARGETS),
        ),
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
    is_global: bool = Form(False),
    db: Session = Depends(get_db),
):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    try:
        column_map = json.loads(column_map_json) if column_map_json.strip() else {}
        if not isinstance(column_map, dict):
            raise ValueError("column_map must be an object")
        column_map = {
            str(k): str(v) for k, v in column_map.items() if v in SUPPORTED_TARGETS
        }
    except (json.JSONDecodeError, ValueError) as e:
        return RedirectResponse(
            url=f"/import-formats?error=Mapping+ungültig:+{e}",
            status_code=status.HTTP_302_FOUND,
        )

    fmt = ImportFormat(
        name=name,
        source_hint=source_hint or "custom",
        separator=separator or ",",
        encoding=encoding or "utf-8",
        date_format=date_format or "%Y-%m-%d",
        time_format=time_format or "%H:%M",
        column_map=column_map,
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
    return templates.TemplateResponse(
        "import_format_edit.html",
        _ctx(
            request,
            user,
            fmt=fmt,
            targets=sorted(SUPPORTED_TARGETS),
            # All currently-mapped headers + the keys from column_map, so the
            # user always sees every assignment they made.
            headers=list(fmt.column_map.keys()),
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

    try:
        column_map = json.loads(column_map_json) if column_map_json.strip() else {}
        if not isinstance(column_map, dict):
            raise ValueError("column_map must be an object")
        column_map = {
            str(k): str(v) for k, v in column_map.items() if v in SUPPORTED_TARGETS
        }
    except (json.JSONDecodeError, ValueError) as e:
        return RedirectResponse(
            url=f"/import-formats?error=Mapping+ungültig:+{e}",
            status_code=status.HTTP_302_FOUND,
        )

    fmt.name = name
    fmt.source_hint = source_hint or "custom"
    fmt.separator = separator or ","
    fmt.encoding = encoding or "utf-8"
    fmt.date_format = date_format or "%Y-%m-%d"
    fmt.time_format = time_format or "%H:%M"
    fmt.column_map = column_map
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
        )
    except ValueError as e:
        formats = _visible_formats(db, user)
        return templates.TemplateResponse(
            "import_run.html",
            _ctx(request, user, formats=formats, result=None, error=str(e), fmt=fmt),
            status_code=400,
        )

    formats = _visible_formats(db, user)
    return templates.TemplateResponse(
        "import_run.html",
        _ctx(request, user, formats=formats, result=result, error=None, fmt=fmt),
    )


# -------------------- Projects --------------------


_KNOWN_SYNC_TARGETS = ["intern", "jira", "salesforce", "bcs", "none"]
_KNOWN_STATUSES = ["active", "inactive"]


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
    return templates.TemplateResponse(
        "projects.html",
        _ctx(
            request,
            user,
            projects=projects,
            sync_targets=_KNOWN_SYNC_TARGETS,
            statuses=_KNOWN_STATUSES,
            flash=flash,
            error=error,
        ),
    )


@router.post("/projects", response_class=HTMLResponse)
def projects_create(
    request: Request,
    name: str = Form(...),
    code: str = Form(...),
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
    p = Project(
        name=name.strip(),
        code=code.strip(),
        customer=(customer.strip() or None),
        color=color or "#6366f1",
        default_sync_target=(
            default_sync_target if default_sync_target in _KNOWN_SYNC_TARGETS else "intern"
        ),
        status=(status_ if status_ in _KNOWN_STATUSES else "active"),
    )
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
            statuses=_KNOWN_STATUSES,
        ),
    )


@router.post("/projects/{project_id}/edit", response_class=HTMLResponse)
def projects_update(
    request: Request,
    project_id: int,
    name: str = Form(...),
    code: str = Form(...),
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
    project.code = code.strip()
    project.customer = customer.strip() or None
    project.color = color or "#6366f1"
    project.default_sync_target = (
        default_sync_target if default_sync_target in _KNOWN_SYNC_TARGETS else "intern"
    )
    project.status = status_ if status_ in _KNOWN_STATUSES else "active"
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
    salesforce_user_id: str = Form(""),
    salesforce_contact_id: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    user.full_name = full_name.strip()
    user.salesforce_user_id = salesforce_user_id.strip() or None
    user.salesforce_contact_id = salesforce_contact_id.strip() or None
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
