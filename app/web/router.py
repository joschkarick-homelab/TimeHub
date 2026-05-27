import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
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


def _ctx(request: Request, user: User, **extra) -> dict:
    return {"request": request, "user": user, "ai_enabled": bool(get_settings().anthropic_api_key), **extra}


@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    user = _maybe_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    recent = list(
        db.execute(
            select(TimeEntry)
            .where(TimeEntry.user_id == user.id)
            .order_by(TimeEntry.entry_date.desc(), TimeEntry.id.desc())
            .limit(20)
        ).scalars()
    )
    projects = list(
        db.execute(select(Project).where(Project.status == "active").order_by(Project.code)).scalars()
    )
    projects_by_id = {p.id: p for p in projects}
    total_minutes = sum(e.duration_minutes for e in recent)
    return templates.TemplateResponse(
        "dashboard.html",
        _ctx(
            request,
            user,
            recent=recent,
            projects=projects,
            projects_by_id=projects_by_id,
            total_hours=round(total_minutes / 60, 2),
        ),
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


@router.post("/entries", response_class=HTMLResponse)
def create_entry(
    entry_date: str = Form(...),
    project_id: int = Form(...),
    duration_minutes: int = Form(...),
    description: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from datetime import date as _date

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=400, detail="project not found")
    entry = TimeEntry(
        user_id=user.id,
        project_id=project_id,
        entry_date=_date.fromisoformat(entry_date),
        duration_minutes=duration_minutes,
        description=description,
    )
    db.add(entry)
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

    return templates.TemplateResponse(
        "import_format_review.html",
        _ctx(
            request,
            user,
            name=name,
            suggestion=suggestion,
            sample_preview="\n".join(text.splitlines()[:10]),
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
        return RedirectResponse(
            url=f"/import?error={e}", status_code=status.HTTP_302_FOUND
        )
    summary = (
        f"{result['created']}+importiert,+{result['failed']}+Fehler"
        if isinstance(result, dict)
        else "fertig"
    )
    return RedirectResponse(url=f"/import?result={summary}", status_code=status.HTTP_302_FOUND)
