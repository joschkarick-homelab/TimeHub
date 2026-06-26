"""Microsoft 365 single sign-on (OIDC) — a login path parallel to the password
form.

Distinct from the calendar connect flow in ``app.web.routes.m365``: this one
asks only for the standard sign-in scopes (``openid profile email``), validates
the returned ID token server-side, then matches it to a TimeHub user (by stable
Entra object id, falling back to e-mail) or auto-provisions a new one, and
finally issues the same session cookie the password login does. Both flows share
the Entra app registration and the PKCE/token mechanics in ``app.services.m365``.
"""

import logging
import secrets
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import create_access_token
from app.services import m365 as m365_svc

log = logging.getLogger(__name__)
router = APIRouter()


def _login_error(msg: str) -> RedirectResponse:
    """Bounce back to the password login screen with a human-readable banner."""
    return RedirectResponse(
        url="/login?error=" + quote_plus(msg)[:300], status_code=status.HTTP_302_FOUND
    )


def _login_redirect_uri(db: Session, request: Request) -> str:
    """Where Microsoft returns the user for SSO. An admin-configured value wins
    (must match the Entra app registration exactly); otherwise derive it from the
    request — fine for a single-host deployment."""
    configured = m365_svc.get_config(db)["login_redirect_uri"]
    if configured:
        return configured
    return str(request.url_for("m365_login_callback"))


@router.get("/auth/m365/login")
def m365_login(request: Request, db: Session = Depends(get_db)):
    if not m365_svc.configured(db):
        return _login_error("Microsoft 365 ist nicht konfiguriert.")
    verifier, challenge = m365_svc.make_pkce()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    redirect_uri = _login_redirect_uri(db, request)
    # Stash CSRF state, PKCE verifier, replay nonce and the exact redirect_uri
    # used (the token exchange must echo the identical value) for the callback.
    request.session["m365_login"] = {
        "state": state,
        "verifier": verifier,
        "nonce": nonce,
        "redirect_uri": redirect_uri,
    }
    try:
        url = m365_svc.authorize_url(
            db,
            state=state,
            code_challenge=challenge,
            redirect_uri=redirect_uri,
            scope=m365_svc.OIDC_SCOPES,
            nonce=nonce,
        )
    except m365_svc.M365Error as e:
        return _login_error(str(e))
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


@router.get("/auth/m365/callback", name="m365_login_callback")
def m365_login_callback(
    request: Request,
    db: Session = Depends(get_db),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    saved = request.session.pop("m365_login", None)
    if error:
        return _login_error(f"Microsoft: {error_description or error}")
    if (
        not code
        or not state
        or not isinstance(saved, dict)
        or not secrets.compare_digest(state, saved.get("state", ""))
    ):
        return _login_error(
            "Microsoft-Anmeldung ungültig oder abgelaufen – bitte erneut versuchen."
        )

    try:
        tokens = m365_svc.exchange_code(
            db,
            code=code,
            code_verifier=saved.get("verifier", ""),
            redirect_uri=saved.get("redirect_uri") or _login_redirect_uri(db, request),
            scope=m365_svc.OIDC_SCOPES,
        )
        id_token = tokens.get("id_token")
        if not id_token:
            raise m365_svc.M365Error("Microsoft hat kein ID-Token geliefert")
        claims = m365_svc.validate_id_token(db, id_token, nonce=saved.get("nonce", ""))
    except m365_svc.M365Error as e:
        return _login_error(str(e))

    profile = m365_svc.profile_from_claims(claims)
    if not profile["email"]:
        return _login_error("Microsoft-Konto ohne E-Mail – Anmeldung nicht möglich.")

    user = _match_or_provision(db, profile)
    if not user.is_active:
        return _login_error(
            "Dein TimeHub-Konto ist deaktiviert – bitte an den Administrator wenden."
        )

    # Same session mechanism as the password login: a signed access token in the
    # session cookie. Everything downstream behaves identically afterwards.
    request.session["access_token"] = create_access_token(user.id)
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)


def _match_or_provision(db: Session, profile: dict) -> User:
    """Resolve the validated SSO identity to a TimeHub user.

    Match by stable Entra object id first, then by e-mail (case-insensitive);
    an unknown account is auto-provisioned as an active, non-admin user (the
    chosen policy). The object id is backfilled on the matched row so future
    logins survive a mailbox/UPN rename.
    """
    oid, email = profile["oid"], profile["email"]
    user: User | None = None
    if oid:
        user = db.execute(select(User).where(User.entra_oid == oid)).scalar_one_or_none()
    if user is None:
        user = db.execute(
            select(User).where(func.lower(User.email) == email)
        ).scalar_one_or_none()

    if user is not None:
        if oid and not user.entra_oid:
            user.entra_oid = oid
            db.add(user)
            db.commit()
        return user

    user = User(
        email=email,
        full_name=profile["name"] or email,
        hashed_password=None,
        is_admin=False,
        is_active=True,
        entra_oid=oid or None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log.info("Auto-provisioned TimeHub user via M365 SSO: %s", email)
    return user
