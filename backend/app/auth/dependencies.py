"""Bearer authentication and reusable role dependencies."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer

from backend.app.auth.models import UserRole
from backend.app.auth.repository import SqlAlchemyUserRepository
from backend.app.auth.tokens import Actor, InvalidAccessToken, TokenService
from backend.app.core.errors import AppError
from backend.app.core.settings import Settings
from backend.app.infrastructure.runtime import RuntimeResources

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


def authentication_error(code: str = "INVALID_TOKEN") -> AppError:
    return AppError(
        code=code,
        http_status=401,
        safe_message="身份认证失败",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_actor(
    request: Request,
    token: Annotated[str | None, Depends(oauth2_scheme)],
) -> Actor:
    if token is None:
        raise authentication_error("AUTHENTICATION_REQUIRED")

    settings: Settings = request.app.state.settings
    resources: RuntimeResources = request.app.state.resources
    try:
        user_id, token_role = TokenService(settings).decode(token)
    except InvalidAccessToken as error:
        raise authentication_error() from error

    async with resources.session_factory() as session:
        user = await SqlAlchemyUserRepository(session).find_by_id(user_id)
    if user is None or not user.is_active or user.role != token_role:
        raise authentication_error()
    return Actor(user_id=user.id, username=user.username, role=user.role)


def ensure_role(actor: Actor, *allowed_roles: UserRole) -> None:
    if actor.role not in allowed_roles:
        raise AppError(
            code="FORBIDDEN",
            http_status=403,
            safe_message="当前用户无权执行此操作",
        )


RoleDependency = Callable[..., Coroutine[Any, Any, Actor]]


def require_roles(*allowed_roles: UserRole) -> RoleDependency:
    async def dependency(
        actor: Annotated[Actor, Depends(get_current_actor)],
    ) -> Actor:
        ensure_role(actor, *allowed_roles)
        return actor

    return dependency
