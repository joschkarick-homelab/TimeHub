from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import Project, TimeEntry, User
from app.security import create_access_token, verify_password

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
        {
            "request": request,
            "user": user,
            "recent": recent,
            "projects": projects,
            "projects_by_id": projects_by_id,
            "total_hours": round(total_minutes / 60, 2),
        },
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
    request: Request,
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
