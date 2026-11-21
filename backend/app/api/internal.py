"""Internal endpoints for the platform, not for app clients.

``GET|POST /internal/cron/tick`` runs one bounded pass of the background work (jobs, periodic
tasks, housekeeping; see ``app/workers/cron.py``). Vercel Cron calls it with ``GET`` and
``Authorization: Bearer $CRON_SECRET``; an external pinger can use either method.

* ``CRON_SECRET`` unset → 404 (the endpoint doesn't exist).
* Missing or wrong bearer → 401. The comparison is constant-time.
"""

from __future__ import annotations

import hmac
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.deps import AppSettings
from app.core.errors import NotFoundError, UnauthorizedError
from app.core.logging import get_logger
from app.db.session import get_database
from app.workers.cron import run_tick

log = get_logger(__name__)

router = APIRouter(prefix="/internal", include_in_schema=False)


def _authorize(request: Request, settings: AppSettings) -> None:
    if settings.cron_secret is None:
        raise NotFoundError("Not found")
    expected = f"Bearer {settings.cron_secret.get_secret_value()}".encode()
    provided = request.headers.get("authorization", "").strip().encode()
    if not hmac.compare_digest(provided, expected):
        log.warning("cron_unauthorized")
        raise UnauthorizedError("Invalid cron credentials", code="invalid_cron_secret")


async def _tick(request: Request, settings: AppSettings) -> JSONResponse:
    _authorize(request, settings)
    result = await run_tick(get_database(), settings)
    body: dict[str, Any] = asdict(result)
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@router.get("/cron/tick")
async def cron_tick_get(request: Request, settings: AppSettings) -> JSONResponse:
    return await _tick(request, settings)


@router.post("/cron/tick")
async def cron_tick_post(request: Request, settings: AppSettings) -> JSONResponse:
    return await _tick(request, settings)
