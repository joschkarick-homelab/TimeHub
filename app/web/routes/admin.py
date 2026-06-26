import logging

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import hash_password
from app.services import app_settings as app_settings_svc
from app.services import m365 as m365_svc
from app.services import salesforce as sf_svc
from app.web.common import (
    _ctx,
    _require_admin,
    templates,
)

log = logging.getLogger(__name__)
router = APIRouter()


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
    m365_cfg = m365_svc.get_config(db)
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
            m365_client_id=m365_cfg["client_id"],
            m365_tenant=m365_cfg["tenant"],
            m365_timezone=m365_cfg["timezone"],
            m365_redirect_uri=m365_cfg["redirect_uri"],
            m365_secret_set=bool(m365_cfg["client_secret"]),
            m365_redirect_hint=str(request.url_for("m365_callback")),
            error=error,
            flash=flash,
        ),
    )


@router.post("/settings/m365", response_class=HTMLResponse)
def settings_m365(
    request: Request,
    m365_client_id: str = Form(""),
    m365_tenant: str = Form(""),
    m365_client_secret: str = Form(""),
    m365_redirect_uri: str = Form(""),
    m365_timezone: str = Form(""),
    db: Session = Depends(get_db),
):
    _require_admin(request, db)
    m365_svc.save_config(
        db,
        client_id=m365_client_id,
        tenant=m365_tenant,
        client_secret=m365_client_secret,
        redirect_uri=m365_redirect_uri,
        timezone=m365_timezone,
    )
    return RedirectResponse(
        url="/users?flash=Microsoft-365-Einstellungen+gespeichert",
        status_code=status.HTTP_302_FOUND,
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

