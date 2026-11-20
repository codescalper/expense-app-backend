"""Pure-ASGI middleware: request id, access log, security headers, request size cap.

Body cap: ``Content-Length`` over the limit is refused up front (fast path); every body is
*also* counted as it streams through ``receive`` so chunked uploads (no Content-Length) can't
exceed it either. FastAPI reads the body before running dependencies (auth, rate limits), so
the cap is what bounds work done for unauthenticated requests. ``/api/v1/auth/*`` only takes
small JSON documents and gets a much smaller cap."""

from __future__ import annotations

import json
import re
import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import get_logger

log = get_logger("app.access")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
# Path segments that carry secrets (an Expo push token in DELETE /notifications/devices/{token}).
_SECRET_PATH_RE = re.compile(r"(/notifications/devices/)[^/]+")


def loggable_path(path: str) -> str:
    """The request path with secret segments (push tokens) replaced by ``<redacted>``."""
    return _SECRET_PATH_RE.sub(r"\1<redacted>", path)


_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")
AUTH_PATH_PREFIX = "/api/v1/auth/"
AUTH_MAX_BODY_BYTES = 64 * 1024


class BodyTooLargeError(HTTPException):
    """Raised from ``receive`` once a streamed body passes the cap. A Starlette
    HTTPException, so FastAPI re-raises it from body parsing and the app's handler renders the
    usual 413 envelope; the middleware also catches it as a fallback."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Request body too large")


class RequestContextMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        hsts: bool,
        max_body_bytes: int,
        auth_max_body_bytes: int = AUTH_MAX_BODY_BYTES,
        auth_path_prefix: str = AUTH_PATH_PREFIX,
    ) -> None:
        self.app = app
        self.hsts = hsts
        self.max_body_bytes = max_body_bytes
        self.auth_max_body_bytes = auth_max_body_bytes
        self.auth_path_prefix = auth_path_prefix

    def _limit_for(self, path: str) -> int:
        if path.startswith(self.auth_path_prefix):
            return min(self.auth_max_body_bytes, self.max_body_bytes)
        return self.max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)
        path: str = scope.get("path", "")
        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                h = MutableHeaders(scope=message)
                h["X-Request-ID"] = request_id
                h["X-Content-Type-Options"] = "nosniff"
                h["X-Frame-Options"] = "DENY"
                h["Referrer-Policy"] = "no-referrer"
                h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                h["Cross-Origin-Opener-Policy"] = "same-origin"
                if not path.startswith(_DOCS_PATHS):
                    h["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
                if path.startswith("/api/") and "cache-control" not in h:
                    h["Cache-Control"] = "no-store"
                if self.hsts:
                    h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
            await send(message)

        limit = self._limit_for(path)
        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLargeError()
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send_wrapper(message)

        try:
            length = headers.get(b"content-length")
            if length is not None and length.isdigit() and int(length) > limit:
                await self._reject_too_large(send_wrapper)
                return
            try:
                await self.app(scope, limited_receive, tracking_send)
            except BodyTooLargeError:
                if response_started:
                    raise
                await self._reject_too_large(send_wrapper)
        finally:
            log.info(
                "request",
                method=scope.get("method"),
                path=loggable_path(path),
                status_code=status_code,
                duration_ms=round((time.perf_counter() - start) * 1000, 1),
            )
            structlog.contextvars.clear_contextvars()

    @staticmethod
    async def _reject_too_large(send: Send) -> None:
        body = json.dumps(
            {"error": {"code": "payload_too_large", "message": "Request body too large"}}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})
