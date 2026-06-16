from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# bcrypt only considers the first 72 bytes; cap here so passwords can't be
# silently truncated (matches MAX_PASSWORD_BYTES in app.security).
_PW_MAX = 72


class UserBase(BaseModel):
    email: EmailStr
    full_name: str = ""
    is_admin: bool = False
    is_active: bool = True
    salesforce_user_id: str | None = None
    salesforce_contact_id: str | None = None


class UserCreate(UserBase):
    password: str = Field(..., min_length=8, max_length=_PW_MAX)


class UserUpdate(BaseModel):
    full_name: str | None = None
    is_admin: bool | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=_PW_MAX)
    salesforce_user_id: str | None = None
    salesforce_contact_id: str | None = None


class UserOut(UserBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
