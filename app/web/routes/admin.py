import logging
import zipfile
from urllib.parse import quote_plus

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import hash_password
from app.services import app_settings as app_settings_svc
from app.services import backup as backup_svc
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
    sf_oauth = sf_svc.get_oauth_config(db)
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
            sf_oauth_client_id=sf_oauth["client_id"],
            sf_oauth_login_url=sf_oauth["login_url"],
            sf_oauth_redirect_uri=sf_oauth["redirect_uri"],
            sf_oauth_secret_set=bool(sf_oauth["client_secret"]),
            sf_oauth_redirect_hint=str(request.url_for("salesforce_oauth_callback")),
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
    base = request.scope.get("root_path", "")
    return RedirectResponse(
        url=f"{base}/users?flash=Microsoft-365-Einstellungen+gespeichert",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/settings/ai-hints", response_class=HTMLResponse)
def settings_ai_hints(request: Request, ai_hints: str = Form(""), db: Session = Depends(get_db)):
    _require_admin(request, db)
    app_settings_svc.set_setting(db, app_settings_svc.AI_HINTS_KEY, ai_hints.strip())
    base = request.scope.get("root_path", "")
    return RedirectResponse(
        url=f"{base}/users?flash=Globale+KI-Vorgaben+gespeichert", status_code=status.HTTP_302_FOUND
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
    base = request.scope.get("root_path", "")
    return RedirectResponse(
        url=f"{base}/users?flash=Salesforce-Zugangsdaten+gespeichert",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/settings/salesforce/oauth", response_class=HTMLResponse)
def settings_salesforce_oauth(
    request: Request,
    sf_oauth_client_id: str = Form(""),
    sf_oauth_client_secret: str = Form(""),
    sf_oauth_login_url: str = Form(""),
    sf_oauth_redirect_uri: str = Form(""),
    db: Session = Depends(get_db),
):
    """Persist the Connected App config for the per-user Salesforce OAuth flow.
    The secret only overwrites when a value is entered (empty keeps existing)."""
    _require_admin(request, db)
    sf_svc.save_oauth_config(
        db,
        client_id=sf_oauth_client_id,
        client_secret=sf_oauth_client_secret,
        login_url=sf_oauth_login_url,
        redirect_uri=sf_oauth_redirect_uri,
    )
    base = request.scope.get("root_path", "")
    return RedirectResponse(
        url=f"{base}/users?flash=Salesforce-OAuth-Einstellungen+gespeichert",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/settings/salesforce/test", response_class=HTMLResponse)
def settings_salesforce_test(request: Request, db: Session = Depends(get_db)):
    """Try a SOAP login against the stored credentials and report the result."""
    _require_admin(request, db)
    base = request.scope.get("root_path", "")
    client = sf_svc.client_from_settings(db)
    if client is None:
        return RedirectResponse(
            url=f"{base}/users?error=Bitte+Username+und+Passwort+hinterlegen",
            status_code=status.HTTP_302_FOUND,
        )
    try:
        client.login()
    except sf_svc.SalesforceError as e:
        return RedirectResponse(
            url=f"{base}/users?error=Salesforce-Login+fehlgeschlagen:+{quote_plus(str(e))[:200]}",
            status_code=status.HTTP_302_FOUND,
        )
    return RedirectResponse(
        url=f"{base}/users?flash=Salesforce-Login+ok+%28{client.instance_url}%29",
        status_code=status.HTTP_302_FOUND,
    )


@router.get("/admin/backup")
def admin_backup(request: Request, db: Session = Depends(get_db)) -> Response:
    _require_admin(request, db)
    data = backup_svc.make_backup_zip(uploads_dir="/app/uploads")
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="timehub-backup.zip"'},
    )


@router.post("/admin/restore")
async def admin_restore(
    request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)
) -> RedirectResponse:
    _require_admin(request, db)
    # Unbounded read / zip-bomb is an accepted risk for this admin-only,
    # interactive maintenance action (no untrusted callers reach this).
    raw = await file.read()
    base = request.scope.get("root_path", "")
    try:
        backup_svc.restore_from_zip(raw, uploads_dir="/app/uploads")
    except (ValueError, zipfile.BadZipFile) as exc:
        return RedirectResponse(url=f"{base}/users?error={quote_plus(str(exc))}", status_code=303)
    return RedirectResponse(
        url=f"{base}/users?flash=Wiederherstellung+erfolgreich+%E2%80%93+bitte+neu+laden",
        status_code=303,
    )


@router.post("/users", response_class=HTMLResponse)
def users_create(
    request: Request,
    email: str = Form(...),
    full_name: str = Form(""),
    password: str = Form(""),
    is_admin: bool = Form(False),
    db: Session = Depends(get_db),
):
    _require_admin(request, db)
    base = request.scope.get("root_path", "")
    # Password is inert under Hub auth (identity comes from X-MSQ-*). Only hash
    # and store it when one was actually supplied; otherwise leave it NULL.
    hashed = None
    if password:
        try:
            hashed = hash_password(password)
        except ValueError as e:
            return RedirectResponse(
                url=f"{base}/users?error=" + quote_plus(str(e)), status_code=status.HTTP_302_FOUND
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
            url=f"{base}/users?error=" + "E-Mail+bereits+vergeben", status_code=status.HTTP_302_FOUND
        )
    return RedirectResponse(url=f"{base}/users", status_code=status.HTTP_302_FOUND)


@router.post("/users/{user_id}/toggle-active", response_class=HTMLResponse)
def users_toggle_active(request: Request, user_id: int, db: Session = Depends(get_db)):
    actor = _require_admin(request, db)
    base = request.scope.get("root_path", "")
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Not found")
    if target.id == actor.id:
        return RedirectResponse(
            url=f"{base}/users?error=Eigenen+Account+nicht+deaktivieren",
            status_code=status.HTTP_302_FOUND,
        )
    target.is_active = not target.is_active
    db.add(target)
    db.commit()
    return RedirectResponse(url=f"{base}/users", status_code=status.HTTP_302_FOUND)


@router.post("/users/{user_id}/toggle-admin", response_class=HTMLResponse)
def users_toggle_admin(request: Request, user_id: int, db: Session = Depends(get_db)):
    actor = _require_admin(request, db)
    base = request.scope.get("root_path", "")
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Not found")
    if target.id == actor.id:
        return RedirectResponse(
            url=f"{base}/users?error=Eigene+Adminrechte+nicht+entziehen",
            status_code=status.HTTP_302_FOUND,
        )
    target.is_admin = not target.is_admin
    db.add(target)
    db.commit()
    return RedirectResponse(url=f"{base}/users", status_code=status.HTTP_302_FOUND)

