import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings

ALGORITHM = "HS256"
API_KEY_PREFIX_LEN = 8
API_KEY_BYTES = 32
# bcrypt silently truncates anything past 72 bytes; reject longer inputs so two
# distinct long passwords can't collide on their shared 72-byte prefix.
MAX_PASSWORD_BYTES = 72

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Marker for application-layer encrypted values, so we can tell ciphertext apart
# from legacy plaintext rows and migrate them lazily on the next write.
_ENC_PREFIX = "enc:1:"


def _fernet() -> Fernet:
    """Symmetric key derived from SECRET_KEY — used to encrypt secrets at rest
    (e.g. the stored Salesforce password/security token)."""
    key = hashlib.sha256(get_settings().secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(plain: str) -> str:
    if not plain:
        return plain
    token = _fernet().encrypt(plain.encode("utf-8")).decode("ascii")
    return _ENC_PREFIX + token


def decrypt_secret(stored: str) -> str:
    """Reverse of encrypt_secret. Legacy plaintext (no marker) is returned as-is
    so existing credentials keep working until their next save. If the key no
    longer matches (SECRET_KEY rotated), return empty rather than leaking the
    raw ciphertext as if it were the secret."""
    if not stored or not stored.startswith(_ENC_PREFIX):
        return stored
    try:
        return _fernet().decrypt(stored[len(_ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        return ""


def hash_password(password: str) -> str:
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"Passwort zu lang (max. {MAX_PASSWORD_BYTES} Bytes)")
    return _pwd.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)


def create_access_token(subject: str | int, extra: dict | None = None) -> str:
    settings = get_settings()
    expire = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)
    payload: dict = {"sub": str(subject), "exp": expire, "iat": datetime.now(UTC)}
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
