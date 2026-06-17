import base64
import binascii
import json
import logging
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db import get_db
from app.models import EntrySync, ImportFormat, Project, TimeEntry, User
from app.schemas.import_format import SUPPORTED_TARGETS
from app.security import create_access_token, hash_password, verify_password
from app.services import app_settings as app_settings_svc
from app.services import entry_sync as es_svc
from app.services import reports as report_svc
from app.services import salesforce as sf_svc
from app.services import sf_push
from app.services import sync_fields as sf
from app.services.ai_mapping import AiMappingError, suggest_mapping
from app.services.csv_import import import_csv
from app.services.sync_rules import load_rules
from app.services.transforms import clean_target_rules, clean_transforms

log = logging.getLogger(__name__)

# ── CSRF protection ──────────────────────────────────────────────────────────
# The web UI authenticates via a session cookie, so every state-changing form
# POST needs a CSRF token. We use a per-session synchronizer token: it lives in
# the (signed, server-side) session, is embedded into pages as a <meta> tag /
# hidden field, and must come back on unsafe requests via the X-CSRF-Token
# header (fetch/XHR) or a `csrf_token` form field.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_FORM_CONTENT_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


def _ensure_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


async def csrf_protect(request: Request) -> None:
    """Router-level guard: mint the session CSRF token on every request (so
    templates can embed it) and verify it on unsafe methods."""
    expected = _ensure_csrf_token(request)
    if request.method in _SAFE_METHODS:
        return
    sent = request.headers.get("X-CSRF-Token")
    if not sent:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(_FORM_CONTENT_TYPES):
            form = await request.form()
            value = form.get("csrf_token")
            sent = value if isinstance(value, str) else None
    if not sent or not secrets.compare_digest(sent, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF-Token fehlt oder ist ungültig",
        )


router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])

# Defensive upper bounds so an unfiltered/huge dataset can't load the entire
# history into memory and render it. High enough not to affect normal use; when
# hit, the templates show a "narrow your filter" banner.
DASHBOARD_ENTRY_CAP = 1000
REPORT_ROW_CAP = 10000

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


class LoginRequired(Exception):
    """Raised when an unauthenticated request hits a protected web page; an
    exception handler turns it into a redirect to /login (registered in
    app.main). Lets route bodies say `user = _require_login(request, db)` in one
    line instead of repeating the maybe-user/redirect dance everywhere."""


def _require_login(request: Request, db: Session) -> User:
    user = _maybe_user(request, db)
    if user is None:
        raise LoginRequired()
    return user


def _require_admin(request: Request, db: Session) -> User:
    user = _require_login(request, db)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin required")
    return user



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


def _filter_query(df: date | None, dt: date | None, project_id: int | None) -> str:
    """Rebuild the dashboard filter as a relative URL, so CRUD actions can
    bounce back to the exact same filtered view instead of resetting to '/'."""
    from urllib.parse import urlencode

    params = {}
    if df is not None:
        params["date_from"] = df.isoformat()
    if dt is not None:
        params["date_to"] = dt.isoformat()
    if project_id is not None:
        params["project_id"] = project_id
    return "/?" + urlencode(params) if params else "/"


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
    flash: str | None = None,
):
    user = _require_login(request, db)

    df = _parse_date(date_from)
    dt = _parse_date(date_to)
    project_id_int: int | None = None
    if project_id:
        try:
            project_id_int = int(project_id)
        except ValueError:
            project_id_int = None
    # Default filter window: the current week (Mon–Sun), so the list is tightly
    # bounded to what you're most likely working on right now.
    if df is None and dt is None and project_id_int is None:
        today = date.today()
        df = today - timedelta(days=today.weekday())
        dt = df + timedelta(days=6)

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
            date_from=df.isoformat() if df else "",
            date_to=dt.isoformat() if dt else "",
            project_id=project_id_int or "",
            filter_query=_filter_query(df, dt, project_id_int),
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
    db: Session = Depends(get_db),
):
    user = _require_login(request, db)
    project = _owned_project_or_404(db, project_id, user)
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
    db.flush()
    es_svc.reconcile_entry_syncs(db, entry, project, load_rules(db))
    db.commit()
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)


def _owned_entry_or_404(db: Session, entry_id: int, user: User) -> TimeEntry:
    entry = db.get(TimeEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")
    # Time data is per-user: only the owner may view or manage an entry.
    if entry.user_id != user.id:
        raise HTTPException(status_code=403, detail="not your entry")
    return entry


def _owned_project_or_404(db: Session, project_id: int, user: User) -> Project:
    """Projects are per-user; only the owner may reference or manage one."""
    project = db.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _safe_next(target: str | None, fallback: str = "/") -> str:
    """Only allow same-site relative redirects (avoid open-redirects)."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return fallback


@router.get("/entries/{entry_id}/edit", response_class=HTMLResponse)
def edit_entry_form(
    request: Request, entry_id: int, db: Session = Depends(get_db),
    error: str | None = None, next: str | None = None,
):
    user = _require_login(request, db)
    entry = _owned_entry_or_404(db, entry_id, user)
    projects = list(
        db.execute(
            select(Project).where(Project.user_id == user.id).order_by(Project.code)
        ).scalars()
    )
    return templates.TemplateResponse(
        "entry_edit.html",
        _ctx(
            request,
            user,
            entry=entry,
            projects=projects,
            next_url=_safe_next(next),
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
        duration = _resolve_duration(start, end, duration_minutes)
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
    db: Session = Depends(get_db),
):
    """Markiere die ausgewählten Einträge als 'manuell erfasst' (sync_status=
    manually_synced). Damit verschwinden sie aus der Sync-Auswahl und werden
    auch vom Stapel-Push übersprungen — gedacht für alte Monate, die schon
    direkt in Salesforce erfasst wurden."""
    user = _require_login(request, db)
    if not entry_ids:
        return RedirectResponse(url="/?error=Keine+Einträge+ausgewählt",
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
    return RedirectResponse(url=f"/?flash={n}+Einträge+als+manuell+erfasst+markiert",
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


# -------------------- User management (admin) --------------------


@router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    db: Session = Depends(get_db),
    error: str | None = None,
    flash: str | None = None,
):
    user = _require_admin(request, db)
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
    _require_admin(request, db)
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
    _require_admin(request, db)
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


@router.post("/settings/salesforce/test", response_class=HTMLResponse)
def settings_salesforce_test(request: Request, db: Session = Depends(get_db)):
    """Try a SOAP login against the stored credentials and report the result."""
    _require_admin(request, db)
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
    _require_admin(request, db)
    try:
        hashed = hash_password(password)
    except ValueError as e:
        from urllib.parse import quote_plus

        return RedirectResponse(
            url="/users?error=" + quote_plus(str(e)), status_code=status.HTTP_302_FOUND
        )
    new = User(
        email=email,
        full_name=full_name,
        hashed_password=hashed,
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
    actor = _require_admin(request, db)
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
    actor = _require_admin(request, db)
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
    "duration_human": "Dauer als Text (1w 2d 3h 4m)",
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


def _sync_dynamic_options(db: Session, user: User | None) -> dict:
    """Runtime-Auswahllisten für SyncFields mit options_source. Aktuell:
    aktive Salesforce-Projektbesetzungen des aktuellen Users (E-Mail-Match).
    Fehler / fehlende Creds → leere Map (UI fällt auf freies Eingabefeld zurück)."""
    options: dict[str, list[dict]] = {}
    if user is None:
        return options
    client = sf_svc.client_from_settings(db)
    if client is None or not user.email:
        return options
    try:
        items = sf_svc.list_assignments_for_user(client, user.email)
    except sf_svc.SalesforceError as e:
        log.info("SF assignment lookup skipped: %s", e)
        return options
    if items:
        options["sf_assignments"] = items
    return options


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
    user = _require_login(request, db)
    formats = _visible_formats(db, user)
    return templates.TemplateResponse(
        "import_formats.html",
        _ctx(request, user, formats=formats, flash=flash, error=error),
    )


@router.get("/import-formats/new", response_class=HTMLResponse)
def formats_new_form(request: Request, db: Session = Depends(get_db)):
    user = _require_login(request, db)
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
    user = _require_login(request, db)

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

    user = _require_login(request, db)

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
    user = _require_login(request, db)

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
    user = _require_login(request, db)
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
    user = _require_login(request, db)
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
    user = _require_login(request, db)
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
    user = _require_login(request, db)
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
    _require_admin(request, db)
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
    user = _require_login(request, db)
    formats = _visible_formats(db, user)
    return templates.TemplateResponse(
        "import_run.html",
        _ctx(request, user, formats=formats, result=result, error=error),
    )


def _run_import(
    db: Session, user: User, fmt: ImportFormat, raw: bytes, apply_target_rules: bool,
    *, dry_run: bool,
) -> dict:
    return import_csv(
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
        dry_run=dry_run,
    )


@router.post("/import/preview", response_class=HTMLResponse)
async def import_preview(
    request: Request,
    format_id: int = Form(...),
    file: UploadFile = File(...),
    apply_target_rules: bool = Form(False),
    db: Session = Depends(get_db),
):
    """Dry-run the import and show what would be created — no rows are written.
    The uploaded CSV is carried into the confirm form (base64) so the actual
    import doesn't require re-selecting the file."""
    user = _require_login(request, db)
    fmt = db.get(ImportFormat, format_id)
    if fmt is None or (not fmt.is_global and fmt.owner_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="format not found")
    raw = await file.read()
    formats = _visible_formats(db, user)
    try:
        result = _run_import(db, user, fmt, raw, apply_target_rules, dry_run=True)
    except ValueError as e:
        return templates.TemplateResponse(
            "import_run.html",
            _ctx(request, user, formats=formats, result=None, error=str(e), fmt=fmt),
            status_code=400,
        )
    return templates.TemplateResponse(
        "import_run.html",
        _ctx(
            request, user, formats=formats, result=None, error=None, fmt=fmt,
            preview=result,
            preview_b64=base64.b64encode(raw).decode("ascii"),
            preview_format_id=format_id,
            preview_apply_target_rules=apply_target_rules,
        ),
    )


@router.post("/import", response_class=HTMLResponse)
async def import_run(
    request: Request,
    format_id: int = Form(...),
    file: UploadFile | None = File(None),
    raw_b64: str = Form(""),
    apply_target_rules: bool = Form(False),
    db: Session = Depends(get_db),
):
    user = _require_login(request, db)
    fmt = db.get(ImportFormat, format_id)
    if fmt is None or (not fmt.is_global and fmt.owner_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="format not found")

    # The CSV comes either freshly uploaded, or carried over from the preview
    # step as base64 in a hidden field (so no re-upload is needed to confirm).
    formats = _visible_formats(db, user)
    if raw_b64:
        try:
            raw = base64.b64decode(raw_b64)
        except (binascii.Error, ValueError):
            return templates.TemplateResponse(
                "import_run.html",
                _ctx(request, user, formats=formats, result=None,
                     error="Vorschau-Daten konnten nicht gelesen werden — bitte erneut hochladen.",
                     fmt=fmt),
                status_code=400,
            )
    elif file is not None:
        raw = await file.read()
    else:
        return templates.TemplateResponse(
            "import_run.html",
            _ctx(request, user, formats=formats, result=None,
                 error="Keine Datei ausgewählt.", fmt=fmt),
            status_code=400,
        )

    try:
        result = _run_import(db, user, fmt, raw, apply_target_rules, dry_run=False)
    except ValueError as e:
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

    return templates.TemplateResponse(
        "import_run.html",
        _ctx(request, user, formats=formats, result=result, error=None, fmt=fmt),
    )


# -------------------- Projects --------------------


_KNOWN_SYNC_TARGETS = ["intern", "jira", "salesforce", "bcs", "none"]
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
            sync_targets=_KNOWN_SYNC_TARGETS,
            sync_field_registry=sf.registry_json("project"),
            sync_dynamic_options=_sync_dynamic_options(db, user),
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
    user = _require_login(request, db)
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
    user = _require_login(request, db)
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
