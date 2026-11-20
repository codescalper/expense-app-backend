from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.api.deps import AuthSvc, Client, CurrentPrincipal
from app.core.rate_limit import ip_rate_limit
from app.schemas.auth import (
    AuthResponse,
    EmailOnlyRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenPair,
    VerifyEmailRequest,
)
from app.schemas.common import MessageResponse

router = APIRouter(prefix="/auth", tags=["auth"])

CHECK_EMAIL = "If the address can be used, we've sent an email with the next step."


@router.post(
    "/register",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(ip_rate_limit("register"))],
)
async def register(body: RegisterRequest, svc: AuthSvc, client: Client) -> MessageResponse:
    """Always 202: the response never reveals whether the email is already registered.
    A 6-digit verification code is emailed; confirm it with /auth/verify-email."""
    await svc.register(body, client)
    return MessageResponse(message=CHECK_EMAIL)


@router.post("/verify-email", dependencies=[Depends(ip_rate_limit("verify_email"))])
async def verify_email(body: VerifyEmailRequest, svc: AuthSvc, client: Client) -> AuthResponse:
    """Confirms the emailed code and signs the user in.

    Clients should also send the ``password`` the user registered with: the code then only
    verifies *that* registration (if someone else re-registered the address meanwhile, this
    fails with ``invalid_code`` and the user simply registers again)."""
    return await svc.verify_email(body, client)


@router.post(
    "/resend-verification",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(ip_rate_limit("resend_verification"))],
)
async def resend_verification(body: EmailOnlyRequest, svc: AuthSvc) -> MessageResponse:
    await svc.resend_verification(body.email)
    return MessageResponse(message=CHECK_EMAIL)


@router.post("/login", dependencies=[Depends(ip_rate_limit("login"))])
async def login(body: LoginRequest, svc: AuthSvc, client: Client) -> AuthResponse:
    return await svc.login(body, client)


@router.post("/refresh", dependencies=[Depends(ip_rate_limit("refresh"))])
async def refresh(body: RefreshRequest, svc: AuthSvc, client: Client) -> TokenPair:
    """Rotates the refresh token. Each refresh token works exactly once."""
    return await svc.refresh(body.refresh_token, client)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(principal: CurrentPrincipal, svc: AuthSvc, client: Client) -> None:
    await svc.logout(principal.user, principal.session_id, client)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(principal: CurrentPrincipal, svc: AuthSvc, client: Client) -> None:
    """Signs out every device, including this one."""
    await svc.logout_all(principal.user, client)


@router.post(
    "/forgot-password",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(ip_rate_limit("forgot_password"))],
)
async def forgot_password(body: EmailOnlyRequest, svc: AuthSvc) -> MessageResponse:
    await svc.forgot_password(body.email)
    return MessageResponse(message=CHECK_EMAIL)


@router.post(
    "/reset-password",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(ip_rate_limit("reset_password"))],
)
async def reset_password(body: ResetPasswordRequest, svc: AuthSvc, client: Client) -> None:
    """Sets a new password using the emailed code and signs out every device."""
    await svc.reset_password(body, client)
