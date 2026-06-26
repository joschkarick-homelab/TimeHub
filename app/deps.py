from datetime import UTC, datetime

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApiKey, User
from app.security import decode_token, hash_api_key

# Write paths a "tracking"-scoped key may reach (everything else is read-only
# for that scope). Kept as a prefix list so it's easy to audit.
_TRACKING_WRITE_PREFIXES = ("/api/v1/time-entries", "/api/v1/timer")


def _key_is_expired(key: ApiKey) -> bool:
    if key.expires_at is None:
        return False
    expires_at = key.expires_at
    # SQLite returns naive datetimes; treat stored values as UTC.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= datetime.now(UTC)


def _lookup_key(raw_key: str, db: Session) -> ApiKey | None:
    """Active (non-revoked, non-expired) key for the given raw token, or None."""
    digest = hash_api_key(raw_key)
    key = db.execute(
        select(ApiKey).where(ApiKey.key_hash == digest, ApiKey.revoked_at.is_(None))
    ).scalar_one_or_none()
    if key is None or _key_is_expired(key):
        return None
    return key


def _user_from_bearer(token: str, db: Session) -> User | None:
    try:
        payload = decode_token(token)
    except ValueError:
        return None
    user_id = payload.get("sub")
    if user_id is None:
        return None
    return db.get(User, int(user_id))


def _api_key_auth(raw_key: str, db: Session) -> tuple[User, str] | None:
    """Authenticate an API key; returns (user, scope) or None. Updates
    last_used_at as a side effect."""
    key = _lookup_key(raw_key, db)
    if key is None:
        return None
    key.last_used_at = datetime.now(UTC)
    db.add(key)
    db.commit()
    user = db.get(User, key.user_id)
    if user is None:
        return None
    return user, key.scope


def api_key_scope(raw_key: str, db: Session) -> str | None:
    """Scope of a valid key without side effects (used by the write-scope
    middleware). None if the key is unknown/revoked/expired."""
    key = _lookup_key(raw_key, db)
    return key.scope if key else None


def scope_allows_write(scope: str, path: str) -> bool:
    """Whether a key with `scope` may perform an unsafe (write) request to
    `path`. Bearer/session auth never reaches this — only API keys do."""
    if scope == "read_write":
        return True
    if scope == "tracking":
        return path.startswith(_TRACKING_WRITE_PREFIXES)
    # "read" (or anything unknown) → no writes
    return False


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> User:
    user: User | None = None

    if authorization and authorization.lower().startswith("bearer "):
        user = _user_from_bearer(authorization[7:].strip(), db)
        # Bearer/JWT (web login, scripts) is full access.
        if user is not None:
            request.state.api_scope = "read_write"

    if user is None and x_api_key:
        result = _api_key_auth(x_api_key.strip(), db)
        if result is not None:
            user, scope = result
            # Expose the scope so the write-scope middleware can enforce it.
            request.state.api_scope = scope

    # Deliberately no session-cookie fallback here: the JSON API authenticates
    # via Bearer token or API key only. Allowing the session cookie would make
    # every state-changing API endpoint reachable (and thus CSRF-able) straight
    # from a logged-in browser. The cookie-authenticated web UI uses its own
    # session reader (`_maybe_user`) plus CSRF protection instead.
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user
