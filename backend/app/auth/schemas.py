"""Public authentication response schemas."""

from uuid import UUID

from pydantic import BaseModel

from backend.app.auth.models import UserRole


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class CurrentUserResponse(BaseModel):
    id: UUID
    username: str
    role: UserRole
