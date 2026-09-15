"""OAuth2 Password Flow and current-user API routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.security import OAuth2PasswordRequestForm

from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.repository import SqlAlchemyUserRepository
from backend.app.auth.schemas import CurrentUserResponse, TokenResponse
from backend.app.auth.service import AuthenticationService, InvalidCredentials
from backend.app.auth.tokens import Actor, TokenService
from backend.app.core.errors import AppError
from backend.app.core.settings import Settings
from backend.app.infrastructure.runtime import RuntimeResources

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/token", response_model=TokenResponse)
async def issue_token(
    request: Request,
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> TokenResponse:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise AppError(
            code="UNSUPPORTED_MEDIA_TYPE",
            http_status=415,
            safe_message="登录接口仅接受 application/x-www-form-urlencoded",
        )

    resources: RuntimeResources = request.app.state.resources
    settings: Settings = request.app.state.settings
    async with resources.session_factory() as session:
        users = SqlAlchemyUserRepository(session)
        try:
            user = await AuthenticationService(users).authenticate(
                form.username,
                form.password,
            )
        except InvalidCredentials as error:
            raise AppError(
                code="INVALID_CREDENTIALS",
                http_status=401,
                safe_message="用户名或密码错误",
                headers={"WWW-Authenticate": "Bearer"},
            ) from error
        await session.commit()

    tokens = TokenService(settings)
    return TokenResponse(
        access_token=tokens.issue(user),
        expires_in=tokens.expires_in_seconds,
    )


@router.get("/me", response_model=CurrentUserResponse)
async def current_user(
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> CurrentUserResponse:
    return CurrentUserResponse(id=actor.user_id, username=actor.username, role=actor.role)
