"""Microsoft 365 OAuth connect/disconnect for the per-user calendar overlay.

The browser-facing half of the integration: it drives the authorization-code +
PKCE flow, stores the resulting tokens on the user's ``M365Connection`` row, and
lets the user disconnect again. The token/Graph mechanics live in
``app.services.m365``; the global app registration is admin-managed (see
app.web.routes.admin)."""

import logging
import secrets
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import M365Connection
from app.services import m365 as m365_svc
from app.web.common import _require_login

log = logging.getLogger(__name__)
router = APIRouter()


def _profile_redirect(*, flash: str | None = None, error: str | None = None) -> RedirectResponse:
    if error:
        url = "/profile?error=" + quote_plus(error)[:300]
    else:
        url = "/profile?flash=" + quote_plus(flash or "")
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


def _redirect_uri(db: Session, request: Request) -> str:
    """Where Microsoft sends the user back. An explicit admin-configured URI
    wins (it must match the Entra app registration exactly); otherwise derive it
    from the request — fine for a single-host deployment."""
    configured = m365_svc.get_config(db)["redirect_uri"]
    if configured:
        return configured
    return str(request.url_for("m365_callback"))


@router.get("/m365/connect")
def m365_connect(request: Request, db: Session = Depends(get_db)):
    _require_login(request, db)
    if not m365_svc.configured(db):
        return _profile_redirect(
            error="Microsoft 365 ist noch nicht konfiguriert – bitte an den Administrator wenden."
        )
    verifier, challenge = m365_svc.make_pkce()
    state = secrets.token_urlsafe(24)
    redirect_uri = _redirect_uri(db, request)
    # Stash PKCE verifier + CSRF state + the exact redirect_uri used (the token
    # exchange must echo the identical value) for the callback to validate.
    request.session["m365_oauth"] = {
        "state": state,
        "verifier": verifier,
        "redirect_uri": redirect_uri,
    }
    try:
        url = m365_svc.authorize_url(
            db, state=state, code_challenge=challenge, redirect_uri=redirect_uri
        )
    except m365_svc.M365Error as e:
        return _profile_redirect(error=str(e))
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


@router.get("/m365/callback", name="m365_callback")
def m365_callback(
    request: Request,
    db: Session = Depends(get_db),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    user = _require_login(request, db)
    saved = request.session.pop("m365_oauth", None)

    if error:
        return _profile_redirect(error=f"Microsoft: {error_description or error}")
    if (
        not code
        or not state
        or not isinstance(saved, dict)
        or not secrets.compare_digest(state, saved.get("state", ""))
    ):
        return _profile_redirect(
            error="Microsoft-Anmeldung ungültig oder abgelaufen – bitte erneut versuchen."
        )

    try:
        tokens = m365_svc.exchange_code(
            db,
            code=code,
            code_verifier=saved.get("verifier", ""),
            redirect_uri=saved.get("redirect_uri") or _redirect_uri(db, request),
        )
        account = m365_svc.fetch_account(tokens["access_token"])
    except m365_svc.M365Error as e:
        return _profile_redirect(error=str(e))

    conn = db.execute(
        select(M365Connection).where(M365Connection.user_id == user.id)
    ).scalar_one_or_none()
    if conn is None:
        conn = M365Connection(user_id=user.id)
    m365_svc.store_tokens(db, conn, tokens, account=account)
    db.add(conn)
    db.commit()
    return _profile_redirect(flash=f"Microsoft 365 verbunden ({account})" if account
                             else "Microsoft 365 verbunden")


@router.post("/m365/disconnect")
def m365_disconnect(request: Request, db: Session = Depends(get_db)):
    user = _require_login(request, db)
    conn = db.execute(
        select(M365Connection).where(M365Connection.user_id == user.id)
    ).scalar_one_or_none()
    if conn is not None:
        db.delete(conn)
        db.commit()
    return _profile_redirect(flash="Microsoft 365 getrennt")
