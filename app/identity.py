"""Agent Hub identity: read X-MSQ-* headers, resolve to a TimeHub user.

Replaces the app's own password/SSO login. The Hub strips inbound X-MSQ-* and
re-sets them from a validated session, so any header we receive is trusted
(contract A.1.4). Missing X-MSQ-User-Id in hub mode = request did not come
through the Hub → unauthenticated.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import get_settings
from app.models import User

log = logging.getLogger(__name__)

ADMIN_ROLE = "AppHub.Admin"
# Reserved internal placeholder domain: synthesized for principals that arrive
# without a real email, so the UNIQUE email column stays satisfiable. Backfilled
# with the real address once the same subject later presents one.
HUB_PLACEHOLDER_DOMAIN = "@hub.local"


@dataclass(frozen=True)
class HubPrincipal:
    subject: str            # X-MSQ-User-Id (stable, opaque)
    email: str | None
    name: str | None
    roles: frozenset[str]
    guest: bool


def principal_from_headers(headers) -> HubPrincipal | None:
    subject = headers.get("x-msq-user-id")
    if not subject:
        return None
    roles = frozenset(
        r.strip() for r in (headers.get("x-msq-roles") or "").split(",") if r.strip()
    )
    return HubPrincipal(
        subject=subject,
        email=(headers.get("x-msq-user-email") or "").lower() or None,
        name=headers.get("x-msq-user-name") or None,
        roles=roles,
        guest=(headers.get("x-msq-guest") == "true"),
    )


def _dev_principal() -> HubPrincipal:
    s = get_settings()
    return HubPrincipal(
        subject="dev-local",
        email=s.dev_user_email.lower(),
        name=s.dev_user_name,
        roles=frozenset({ADMIN_ROLE}) if s.dev_user_admin else frozenset(),
        guest=False,
    )


def _should_be_admin(principal: HubPrincipal) -> bool:
    if ADMIN_ROLE in principal.roles:
        return True
    return bool(principal.email and principal.email in get_settings().admin_email_set)


def _apply_existing(db: Session, user: User, principal: HubPrincipal) -> User:
    """Reconcile an already-known user with the current principal: backfill the
    Hub subject, grant admin, replace a placeholder email. Commits only if
    something actually changed. full_name is deliberately NOT reconciled here."""
    changed = False
    if user.msq_user_id != principal.subject:
        user.msq_user_id = principal.subject
        changed = True
    # Grant-only: we never auto-revoke is_admin on login, because admin can also
    # be granted in-app (admin users page: app/web/routes/admin.py) and a
    # login-revoke would clobber that. Removing the Hub allowlist/role does NOT
    # demote — do that in the users page or the DB.
    if _should_be_admin(principal) and not user.is_admin:
        user.is_admin = True
        changed = True
    # full_name is set on provision only; the in-app profile is authoritative
    # afterwards, so a Hub display-name change does not overwrite a user's edit
    # (same grant-only philosophy as is_admin).
    # Replace the internal placeholder once a real email shows up for this user.
    if user.email.endswith(HUB_PLACEHOLDER_DOMAIN) and principal.email:
        user.email = principal.email
        changed = True
    if changed:
        db.add(user)
        db.commit()
    return user


def resolve_user(db: Session, principal: HubPrincipal) -> User:
    """Match by msq_user_id, then email; provision if unknown. Admin status is
    re-evaluated on every login so allowlist/role changes take effect."""
    user = db.execute(
        select(User).where(User.msq_user_id == principal.subject)
    ).scalar_one_or_none()
    if user is None and principal.email:
        user = db.execute(
            select(User).where(func.lower(User.email) == principal.email)
        ).scalar_one_or_none()

    if user is not None:
        return _apply_existing(db, user, principal)

    user = User(
        email=principal.email or f"{principal.subject}{HUB_PLACEHOLDER_DOMAIN}",
        full_name=principal.name or principal.email or principal.subject,
        msq_user_id=principal.subject,
        is_active=True,
        is_admin=_should_be_admin(principal),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Lost a concurrent first-touch race — the winner already inserted the
        # row under our UNIQUE msq_user_id/email. Re-select and reconcile.
        existing = db.execute(
            select(User).where(User.msq_user_id == principal.subject)
        ).scalar_one_or_none()
        if existing is None and principal.email:
            existing = db.execute(
                select(User).where(func.lower(User.email) == principal.email)
            ).scalar_one_or_none()
        if existing is None:
            raise
        return _apply_existing(db, existing, principal)
    db.refresh(user)
    log.info("Provisioned TimeHub user from Hub identity: %s", user.email)
    return user


def principal_for_request(request: Request) -> HubPrincipal | None:
    """Dev-bypass injects a fixed identity; otherwise read the Hub headers."""
    if get_settings().resolved_auth_mode == "dev-bypass":
        return _dev_principal()
    return principal_from_headers(request.headers)


def resolve_request_user(request: Request, db: Session) -> User | None:
    """Resolve (and cache on request.state) the TimeHub user for this request.
    Returns None when no Hub identity is present (caller raises 401/redirect).
    Called lazily by the web/API readers — no global middleware, so the MCP
    SSE stream is never wrapped by BaseHTTPMiddleware."""
    cached = getattr(request.state, "hub_user_id", None)
    if cached is not None:
        return db.get(User, cached)
    principal = principal_for_request(request)
    if principal is None:
        return None
    user = resolve_user(db, principal)
    request.state.hub_user_id = user.id
    request.state.hub_is_guest = principal.guest
    return user
