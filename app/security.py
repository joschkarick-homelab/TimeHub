import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings

ALGORITHM = "HS256"
API_KEY_PREFIX_LEN = 8
API_KEY_BYTES = 32

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)


def create_access_token(subject: str | int, extra: dict | None = None) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload: dict = {"sub": str(subject), "exp": expire, "iat": datetime.now(timezone.utc)}
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError as e:
        raise ValueError("invalid token") from e


def generate_api_key() -> tuple[str, str, str]:
    """Return (full_key, prefix, sha256_hash). Store prefix + hash, give full_key to the user once."""
    raw = secrets.token_urlsafe(API_KEY_BYTES)
    full = f"thk_{raw}"
    prefix = full[:API_KEY_PREFIX_LEN]
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()
    return full, prefix, digest


def hash_api_key(full_key: str) -> str:
    return hashlib.sha256(full_key.encode("utf-8")).hexdigest()
