"""API-contract schemas for authentication."""

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
