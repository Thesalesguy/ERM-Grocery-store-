"""API-contract schemas for authentication."""

from decimal import Decimal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int


class CurrentUserResponse(BaseModel):
    id: int
    username: str
    full_name: str
    store_id: int | None
    permissions: list[str]


class UserDeactivateResponse(BaseModel):
    id: int
    username: str
    is_active: bool


class UserRead(BaseModel):
    id: int
    username: str
    email: str
    full_name: str
    store_id: int | None
    is_active: bool
    role: str | None


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=255)
    full_name: str = Field(min_length=1, max_length=255)
    store_id: int | None = None
    role: str = Field(min_length=1)


class UserRoleUpdateRequest(BaseModel):
    role: str = Field(min_length=1)


class RoleRead(BaseModel):
    name: str
    description: str


class StoreSettingsRead(BaseModel):
    id: int
    name: str
    address: str | None
    timezone: str
    is_active: bool
    attendance_day_boundary_hour: int
    return_approval_threshold_amount: Decimal | None


class StoreSettingsUpdateRequest(BaseModel):
    """All fields optional -- only what's provided is changed
    (update_store_settings' own `_UNSET` sentinel distinguishes an
    omitted `return_approval_threshold_amount` from an explicit `null`,
    which clears the threshold rather than leaving it unchanged)."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    address: str | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    attendance_day_boundary_hour: int | None = Field(default=None, ge=0, le=23)
    return_approval_threshold_amount: Decimal | None = Field(default=None)
    clear_return_approval_threshold: bool = False
