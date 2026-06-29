"""Per-user Salesforce OAuth connect/disconnect.

The browser-facing half of the per-user Salesforce link: it drives the OAuth
web-server flow (authorization code + PKCE), stores the resulting tokens on the
user's ``SalesforceConnection`` row, and lets the user disconnect again. Because
the org's Salesforce login delegates to Microsoft 365 SSO, the consent screen is
effectively an M365 sign-in. Token/HTTP mechanics live in
``app.services.salesforce``; the Connected App is admin-managed (see
``app.web.routes.admin``)."""

import logging
import secrets
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import SalesforceConnection
from app.services import salesforce as sf_svc
from app.web.common import _require_login

log = logging.getLogger(__name__)
router = APIRouter()


def _profile_redirect(
    request: Request, *, flash: str | None = None, error: str | None = None
) -> RedirectResponse:
    base = request.scope.get("root_path", "")
    if error:
        url = f"{base}/profile?error=" + quote_plus(error)[:300]
    else:
        url = f"{base}/profile?flash=" + quote_plus(flash or "")
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


def _redirect_uri(db: Session, request: Request) -> str:
    """Where Salesforce returns the user. An admin-configured URI wins (must
    match the Connected App's callback exactly); otherwise derive it from the
    request — fine for a single-host deployment."""
    configured = sf_svc.get_oauth_config(db)["redirect_uri"]
    if configured:
        return configured
    return str(request.url_for("salesforce_oauth_callback"))


@router.get("/salesforce/oauth/connect")
def salesforce_oauth_connect(request: Request, db: Session = Depends(get_db)):
    _require_login(request, db)
    if not sf_svc.oauth_configured(db):
        return _profile_redirect(
            request, error="Salesforce-OAuth ist noch nicht konfiguriert – bitte an den Administrator wenden."
        )
    verifier, challenge = sf_svc.make_pkce()
    state = secrets.token_urlsafe(24)
    redirect_uri = _redirect_uri(db, request)
    # Stash PKCE verifier + CSRF state + the exact redirect_uri used (the token
    # exchange must echo the identical value) for the callback to validate.
    request.session["sf_oauth"] = {
        "state": state,
        "verifier": verifier,
        "redirect_uri": redirect_uri,
    }
    try:
        url = sf_svc.oauth_authorize_url(
            db, state=state, code_challenge=challenge, redirect_uri=redirect_uri
        )
    except sf_svc.SalesforceError as e:
        return _profile_redirect(request, error=str(e))
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


@router.get("/salesforce/oauth/callback", name="salesforce_oauth_callback")
def salesforce_oauth_callback(
    request: Request,
    db: Session = Depends(get_db),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    user = _require_login(request, db)
    saved = request.session.pop("sf_oauth", None)

    if error:
        return _profile_redirect(request, error=f"Salesforce: {error_description or error}")
    if (
        not code
        or not state
        or not isinstance(saved, dict)
        or not secrets.compare_digest(state, saved.get("state", ""))
    ):
        return _profile_redirect(
            request, error="Salesforce-Anmeldung ungültig oder abgelaufen – bitte erneut versuchen."
        )

    try:
        tokens = sf_svc.oauth_exchange_code(
            db,
            code=code,
            code_verifier=saved.get("verifier", ""),
            redirect_uri=saved.get("redirect_uri") or _redirect_uri(db, request),
        )
        account = sf_svc.oauth_userinfo(
            tokens.get("access_token", ""), tokens.get("instance_url", "")
        )
    except sf_svc.SalesforceError as e:
        return _profile_redirect(request, error=str(e))

    conn = db.execute(
        select(SalesforceConnection).where(SalesforceConnection.user_id == user.id)
    ).scalar_one_or_none()
    if conn is None:
        conn = SalesforceConnection(user_id=user.id)
    sf_svc.store_oauth_tokens(db, conn, tokens, account=account)
    db.add(conn)
    db.commit()
    return _profile_redirect(
        request, flash=f"Salesforce verbunden ({account})" if account else "Salesforce verbunden"
    )


@router.post("/salesforce/oauth/disconnect")
def salesforce_oauth_disconnect(request: Request, db: Session = Depends(get_db)):
    user = _require_login(request, db)
    conn = db.execute(
        select(SalesforceConnection).where(SalesforceConnection.user_id == user.id)
    ).scalar_one_or_none()
    if conn is not None:
        db.delete(conn)
        db.commit()
    return _profile_redirect(request, flash="Salesforce getrennt")
