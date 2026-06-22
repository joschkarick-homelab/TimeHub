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
from app.services import bcs as bcs_svc
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
    bcs_creds = bcs_svc.get_credentials(db)
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
            bcs_username=bcs_creds["username"],
            bcs_wsdl_url=bcs_creds["wsdl_url"],
            bcs_password_set=bool(bcs_creds["password"]),
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


@router.post("/settings/bcs", response_class=HTMLResponse)
def settings_bcs(
    request: Request,
    bcs_username: str = Form(""),
    bcs_password: str = Form(""),
    bcs_wsdl_url: str = Form(""),
    db: Session = Depends(get_db),
):
    _require_admin(request, db)
    bcs_svc.save_credentials(
        db, username=bcs_username, password=bcs_password, wsdl_url=bcs_wsdl_url,
    )
    return RedirectResponse(
        url="/users?flash=BCS-Zugangsdaten+gespeichert",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/settings/bcs/test", response_class=HTMLResponse)
def settings_bcs_test(
    request: Request,
    bcs_impersonate: str = Form(""),
    db: Session = Depends(get_db),
):
    """Smoke-test the stored BCS credentials with a GetTimeTrackingSettings call.
    Optionally impersonate a consultant e-mail to verify the impersonation
    setup end to end."""
    from urllib.parse import quote_plus
    actor = _require_admin(request, db)
    client = bcs_svc.client_from_settings(db)
    if client is None:
        return RedirectResponse(
            url="/users?error=Bitte+Username,+Passwort+und+WSDL-URL+hinterlegen",
            status_code=status.HTTP_302_FOUND,
        )
    impersonate = (bcs_impersonate.strip() or actor.email) or None
    try:
        client.test_connection(impersonate=impersonate)
    except bcs_svc.BcsError as e:
        return RedirectResponse(
            url=f"/users?error=BCS-Verbindung+fehlgeschlagen:+{quote_plus(str(e))[:200]}",
            status_code=status.HTTP_302_FOUND,
        )
    who = quote_plus(impersonate or "")
    return RedirectResponse(
        url=f"/users?flash=BCS-Verbindung+ok+%28ImpersonateAs:+{who}%29",
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

