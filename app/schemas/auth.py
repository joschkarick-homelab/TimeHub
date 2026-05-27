from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ApiKeyCreate(BaseModel):
    name: str


class ApiKeyOut(BaseModel):
    id: int
    name: str
    prefix: str
    last_used_at: str | None = None
    created_at: str
    revoked_at: str | None = None


class ApiKeyCreated(ApiKeyOut):
    key: str  # full token, returned once
