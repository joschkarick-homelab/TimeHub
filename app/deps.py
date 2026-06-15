from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApiKey, User
from app.security import decode_token, hash_api_key


def _user_from_bearer(token: str, db: Session) -> User | None:
    try:
        payload = decode_token(token)
    except ValueError:
        return None
    user_id = payload.get("sub")
    if user_id is None:
        return None
    return db.get(User, int(user_id))


def _user_from_api_key(raw_key: str, db: Session) -> User | None:
    digest = hash_api_key(raw_key)
    stmt = select(ApiKey).where(ApiKey.key_hash == digest, ApiKey.revoked_at.is_(None))
    key = db.execute(stmt).scalar_one_or_none()
    if key is None:
        return None
    key.last_used_at = datetime.now(timezone.utc)
    db.add(key)
    db.commit()
    return db.get(User, key.user_id)


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> User:
    user: User | None = None

    if authorization and authorization.lower().startswith("bearer "):
        user = _user_from_bearer(authorization[7:].strip(), db)

    if user is None and x_api_key:
        user = _user_from_api_key(x_api_key.strip(), db)

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
