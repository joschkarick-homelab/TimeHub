import logging

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Request,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import create_access_token, verify_password
from app.web.common import (
    _THEMES,
    _ctx,
    _require_login,
    templates,
)

log = logging.getLogger(__name__)
router = APIRouter()


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

