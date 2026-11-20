"""Application error types and the uniform JSON error envelope.

Every error response looks like::

    {"error": {"code": "not_found", "message": "...", "details": ...}}
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

log = get_logger(__name__)


class AppError(Exception):
    status_code = 400
    code = "bad_request"
    message = "Bad request"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details
        self.headers = headers
        super().__init__(self.message)


class BadRequestError(AppError):
    pass


class ValidationFailedError(AppError):
    status_code = 422
    code = "validation_error"
    message = "Invalid request"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"
    message = "Authentication required"

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        super().__init__(message, code=code, headers={"WWW-Authenticate": "Bearer"})


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"
    message = "You do not have permission to perform this action"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    message = "Resource not found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    message = "Conflict"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"
    message = "Payload too large"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"
    message = "Too many requests. Please try again later."

    def __init__(self, retry_after: int, message: str | None = None) -> None:
        super().__init__(
            message,
            details={"retry_after_seconds": retry_after},
            headers={"Retry-After": str(max(retry_after, 1))},
        )


def _envelope(code: str, message: str, details: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return {"error": body}


_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            _envelope(exc.code, exc.message, exc.details),
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo the submitted input back (it may contain passwords).
        details = [
            {
                "field": ".".join(str(p) for p in err.get("loc", ()) if p != "body"),
                "message": err.get("msg", "Invalid value"),
                "type": err.get("type", "value_error"),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            _envelope("validation_error", "Invalid request", details), status_code=422
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "error")
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return JSONResponse(
            _envelope(code, message), status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", error_type=type(exc).__name__)
        return JSONResponse(
            _envelope("internal_error", "An unexpected error occurred"), status_code=500
        )
