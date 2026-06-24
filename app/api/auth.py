from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import ApiKey, User
from app.schemas.auth import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, LoginRequest, TokenResponse
from app.schemas.user import UserOut
from app.security import create_access_token, generate_api_key, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return TokenResponse(access_token=create_access_token(user.id))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


def _serialize_key(k: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=k.id,
        name=k.name,
        prefix=k.prefix,
        last_used_at=k.last_used_at.isoformat() if k.last_used_at else None,
        created_at=k.created_at.isoformat(),
        revoked_at=k.revoked_at.isoformat() if k.revoked_at else None,
    )


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=201)
def create_api_key(
    payload: ApiKeyCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    full, prefix, digest = generate_api_key()
    key = ApiKey(user_id=user.id, name=payload.name, prefix=prefix, key_hash=digest)
    db.add(key)
    db.commit()
    db.refresh(key)
    base = _serialize_key(key)
    return ApiKeyCreated(**base.model_dump(), key=full)


@router.get("/api-keys", response_model=list[ApiKeyOut])
def list_api_keys(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    stmt = select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
    return [_serialize_key(k) for k in db.execute(stmt).scalars()]


@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_api_key(
    key_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    key = db.get(ApiKey, key_id)
    if key is None or key.user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        db.add(key)
        db.commit()
    return None
