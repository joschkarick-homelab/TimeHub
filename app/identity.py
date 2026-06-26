"""Agent Hub identity: read X-MSQ-* headers, resolve to a TimeHub user.

Replaces the app's own password/SSO login. The Hub strips inbound X-MSQ-* and
re-sets them from a validated session, so any header we receive is trusted
(contract A.1.4). Missing X-MSQ-User-Id in hub mode = request did not come
through the Hub → unauthenticated.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import get_settings
from app.models import User

log = logging.getLogger(__name__)

ADMIN_ROLE = "AppHub.Admin"


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

    if user is None:
        user = User(
            email=principal.email or f"{principal.subject}@hub.local",
            full_name=principal.name or principal.email or principal.subject,
            msq_user_id=principal.subject,
            is_active=True,
            is_admin=_should_be_admin(principal),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        log.info("Provisioned TimeHub user from Hub identity: %s", user.email)
        return user

    changed = False
    if user.msq_user_id != principal.subject:
        user.msq_user_id = principal.subject
        changed = True
    admin = _should_be_admin(principal)
    if admin and not user.is_admin:
        user.is_admin = True
        changed = True
    if principal.name and user.full_name != principal.name:
        user.full_name = principal.name
        changed = True
    if changed:
        db.add(user)
        db.commit()
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
