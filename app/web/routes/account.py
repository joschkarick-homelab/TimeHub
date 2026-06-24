import logging
from datetime import UTC, datetime

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
from app.models import ApiKey, User
from app.security import create_access_token, generate_api_key, verify_password
from app.web.common import (
    _DEFAULT_THEME,
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
        theme = _DEFAULT_THEME
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
    api_keys = list(
        db.execute(
            select(ApiKey)
            .where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
            .order_by(ApiKey.created_at.desc())
        ).scalars()
    )
    # The full key is only available once, right after creation. We stash it in
    # the session so a redirect can reveal it a single time, then drop it.
    new_api_key = request.session.pop("new_api_key", None)
    new_api_key_name = request.session.pop("new_api_key_name", None)
    return templates.TemplateResponse(
        "profile.html",
        _ctx(
            request,
            user,
            flash=flash,
            error=error,
            api_keys=api_keys,
            new_api_key=new_api_key,
            new_api_key_name=new_api_key_name,
        ),
    )


@router.post("/profile/api-keys")
def create_profile_api_key(
    request: Request,
    name: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _require_login(request, db)
    label = name.strip() or "API Key"
    full, prefix, digest = generate_api_key()
    key = ApiKey(user_id=user.id, name=label, prefix=prefix, key_hash=digest)
    db.add(key)
    db.commit()
    # Revealed once on the next /profile render (see profile_page).
    request.session["new_api_key"] = full
    request.session["new_api_key_name"] = label
    return RedirectResponse(
        url="/profile?flash=API-Key+erstellt", status_code=status.HTTP_302_FOUND
    )


@router.post("/profile/api-keys/{key_id}/revoke")
def revoke_profile_api_key(
    key_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _require_login(request, db)
    key = db.get(ApiKey, key_id)
    if key is None or key.user_id != user.id:
        return RedirectResponse(
            url="/profile?error=API-Key+nicht+gefunden", status_code=status.HTTP_302_FOUND
        )
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        db.add(key)
        db.commit()
    return RedirectResponse(
        url="/profile?flash=API-Key+widerrufen", status_code=status.HTTP_302_FOUND
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

