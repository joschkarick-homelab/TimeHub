from typing import Literal

from pydantic import BaseModel, EmailStr, Field

ApiKeyScope = Literal["read", "tracking", "read_write"]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ApiKeyCreate(BaseModel):
    name: str
    # read = GET only; tracking = read + write time-entries & timer;
    # read_write = full. Defaults to full for back-compat with existing callers.
    scope: ApiKeyScope = "read_write"
    # Optional lifetime in days; omitted = never expires.
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiKeyOut(BaseModel):
    id: int
    name: str
    prefix: str
    scope: ApiKeyScope = "read_write"
    expires_at: str | None = None
    last_used_at: str | None = None
    created_at: str
    revoked_at: str | None = None


class ApiKeyCreated(ApiKeyOut):
    key: str  # full token, returned once
