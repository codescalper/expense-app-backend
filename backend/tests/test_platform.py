"""Health, middleware, config, security helpers, rate limiter and the job worker."""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from app.core import security
from app.core.config import Settings, get_settings, normalize_async_db_url
from app.core.errors import ValidationFailedError
from app.core.logging import REDACTED, configure_logging, redact
from app.core.security import ensure_password_policy, hash_password, utcnow, verify_password
from app.db.session import build_engine, get_database
from app.main import MAX_REQUEST_BYTES
from app.models.system import BackgroundJob, JobStatus
from app.repositories.system import JobRepository
from app.services.email import NOOP_EMAIL_JOB
from app.workers.worker import HANDLERS, process_due_jobs, run_housekeeping


async def test_health_endpoints(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    r = await client.get("/health/ready")
    assert r.status_code == 200 and r.json()["database"] == "up"


async def test_security_headers_and_request_id(client):
    r = await client.get("/api/v1/users/me", headers={"X-Request-ID": "abc12345-req"})
    assert r.headers["x-request-id"] == "abc12345-req"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
    assert "content-security-policy" in r.headers
    # Malformed request ids are replaced, not reflected.
    r = await client.get("/health", headers={"X-Request-ID": "<script>"})
    assert r.headers["x-request-id"] != "<script>"


async def test_cors_allowlist(client):
    ok = await client.options(
        "/api/v1/auth/login",
        headers={"Origin": "http://localhost:8081", "Access-Control-Request-Method": "POST"},
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:8081"
    bad = await client.options(
        "/api/v1/auth/login",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in bad.headers


async def test_oversized_body_rejected(client):
    r = await client.post(
        "/api/v1/auth/login",
        content=b"x",
        headers={"Content-Length": str(50 * 1024 * 1024), "Content-Type": "application/json"},
    )
    assert r.status_code == 413


def test_neon_url_normalisation():
    url, args = normalize_async_db_url(
        "postgresql://u:p@ep-x-pooler.ap-southeast-1.aws.neon.tech/db?sslmode=require&channel_binding=require"
    )
    assert url == "postgresql+asyncpg://u:p@ep-x-pooler.ap-southeast-1.aws.neon.tech/db"
    assert args == {"ssl": "require"}
    url, args = normalize_async_db_url("postgresql+asyncpg://u:p@localhost:5432/db")
    assert url.endswith("localhost:5432/db") and args == {}


def test_production_rejects_placeholder_secret():
    base: dict[str, Any] = {"database_url": "postgresql+asyncpg://x/y", "environment": "production"}
    with pytest.raises(ValidationError):
        Settings(jwt_secret_key="dev-only-insecure-secret-change-me-0123456789abcdef", **base)
    with pytest.raises(ValidationError):
        Settings(jwt_secret_key="too-short", **base)
    with pytest.raises(ValidationError):
        Settings(jwt_secret_key="R" * 48, cors_origins="*", **base)
    assert Settings(jwt_secret_key="R" * 48, **base).is_production_like


def test_password_hashing_is_argon2id():
    h = hash_password("some long passphrase")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "some long passphrase")
    assert not verify_password(h, "some long passphrasE")
    assert not verify_password("!", "anything")


def test_password_policy():
    ensure_password_policy("correct horse battery staple", "asha@example.com")
    for bad in ("short1", "password123", "zzzzzzzzzzzz", "my-asha-password"):
        with pytest.raises(ValidationFailedError):
            ensure_password_policy(bad, "asha@example.com")


def test_log_redaction():
    out = redact({"password": "x", "nested": {"refresh_token": "y", "code": "123456"}, "ok": 1})
    assert out["password"] == REDACTED
    assert out["nested"]["refresh_token"] == REDACTED
    assert out["nested"]["code"] == REDACTED
    assert out["ok"] == 1
    assert redact({"status_code": 200}) == {"status_code": 200}


# --- worker -----------------------------------------------------------------------------


async def _enqueue(kind: str, payload: dict[str, Any], **kw: Any) -> None:
    async with get_database().sessionmaker() as s:
        await JobRepository(s).enqueue(kind, payload, **kw)
        await s.commit()


async def _jobs() -> list[BackgroundJob]:
    async with get_database().sessionmaker() as s:
        return list(await s.scalars(select(BackgroundJob).order_by(BackgroundJob.created_at)))


async def test_worker_runs_job_and_wipes_payload(settings):
    seen: list[dict[str, Any]] = []

    async def ok(payload: dict[str, Any], _s: Settings) -> None:
        seen.append(payload)

    await _enqueue("demo", {"secret": "123456"})
    assert await process_due_jobs(get_database(), settings, {"demo": ok}) == 1
    [job] = await _jobs()
    assert seen == [{"secret": "123456"}]
    assert job.status == JobStatus.DONE and job.payload == {}


async def test_worker_retries_then_fails(settings):
    async def boom(_p: dict[str, Any], _s: Settings) -> None:
        raise RuntimeError("smtp down")

    await _enqueue("demo", {}, max_attempts=2)
    await process_due_jobs(get_database(), settings, {"demo": boom})
    [job] = await _jobs()
    assert job.status == JobStatus.PENDING and job.run_at > utcnow()
    assert "smtp down" in (job.last_error or "")

    async with get_database().engine.begin() as conn:  # make it due again
        await conn.execute(text("UPDATE background_jobs SET run_at = now() - interval '1 second'"))
    await process_due_jobs(get_database(), settings, {"demo": boom})
    [job] = await _jobs()
    assert job.status == JobStatus.FAILED and job.attempts == 2


async def test_dedupe_key_prevents_duplicate_jobs():
    await _enqueue("reminder", {}, dedupe_key="bill:1:2026-11")
    await _enqueue("reminder", {}, dedupe_key="bill:1:2026-11")
    assert len(await _jobs()) == 1


async def test_future_jobs_not_run_early(settings):
    await _enqueue("demo", {}, run_at=utcnow() + timedelta(hours=1))
    assert await process_due_jobs(get_database(), settings, {}) == 0


async def test_housekeeping_purges_expired_counters():
    async with get_database().engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO rate_limit_counters (key, window_start, count, expires_at) VALUES "
                "('old', now() - interval '2 hours', 3, now() - interval '1 hour'),"
                "('live', now(), 1, now() + interval '1 hour')"
            )
        )
    counts = await run_housekeeping(get_database())
    assert counts["rate_limit_counters"] == 1


# --- regression tests: security review -------------------------------------------------------


async def _chunks(total: int, size: int = 16 * 1024) -> AsyncIterator[bytes]:
    sent = 0
    while sent < total:
        n = min(size, total - sent)
        sent += n
        yield b"x" * n


async def test_chunked_body_over_auth_cap_rejected(client):
    """No Content-Length (chunked): the cap is enforced while the body streams in."""
    r = await client.post(
        "/api/v1/auth/login",
        content=_chunks(100 * 1024),
        headers={"Content-Type": "application/json"},
    )
    assert "content-length" not in r.request.headers
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"


async def test_chunked_body_over_general_cap_rejected(client):
    """A well-formed multipart upload streamed without Content-Length, larger than the
    general cap, is cut off while streaming (FastAPI reads it before auth runs)."""

    async def upload() -> AsyncIterator[bytes]:
        yield (
            b"--xyz\r\n"
            b'Content-Disposition: form-data; name="file"; filename="a.png"\r\n'
            b"Content-Type: image/png\r\n\r\n"
        )
        async for chunk in _chunks(MAX_REQUEST_BYTES + 64 * 1024, size=256 * 1024):
            yield chunk
        yield b"\r\n--xyz--\r\n"

    r = await client.put(
        "/api/v1/users/me/avatar",
        content=upload(),
        headers={"Content-Type": "multipart/form-data; boundary=xyz"},
    )
    assert "content-length" not in r.request.headers
    assert r.status_code == 413


async def test_auth_endpoints_have_small_content_length_cap(client):
    r = await client.post(
        "/api/v1/auth/login",
        content=b"{" + b" " * (70 * 1024) + b"}",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413
    # A normal-sized chunked body still works.
    body = b'{"email": "a@example.com", "password": "whatever it is"}'

    async def small() -> AsyncIterator[bytes]:
        yield body[:10]
        yield body[10:]

    r = await client.post(
        "/api/v1/auth/login", content=small(), headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 401


def test_production_rejects_wildcard_forwarded_allow_ips():
    base: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://x/y",
        "environment": "production",
        "jwt_secret_key": "R" * 48,
    }
    with pytest.raises(ValidationError):
        Settings(forwarded_allow_ips="10.0.0.1, *", **base)
    assert Settings(forwarded_allow_ips="10.0.0.0/8", **base).forwarded_allow_ips_list == [
        "10.0.0.0/8"
    ]
    dev = {**base, "environment": "development"}
    assert Settings(forwarded_allow_ips="*", **dev).forwarded_allow_ips == "*"


async def test_engine_hides_sql_parameters():
    engine = build_engine(get_settings())
    try:
        assert engine.sync_engine.hide_parameters is True
    finally:
        await engine.dispose()


def test_dev_console_logs_never_print_local_variables():
    configure_logging("development")
    stream = io.StringIO()
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(stream)
    try:

        def failing_login() -> None:
            password_in_a_local = "hunter2-should-never-be-logged"  # noqa: F841
            raise ValueError("boom")

        try:
            failing_login()
        except ValueError:
            logging.getLogger("app.test").exception("login_failed")
        out = stream.getvalue()
        assert "ValueError" in out and "Traceback" in out
        assert "hunter2-should-never-be-logged" not in out
    finally:
        configure_logging("test")


async def test_worker_drops_expired_jobs_and_wipes_payload(settings):
    sent: list[dict[str, Any]] = []

    async def send(payload: dict[str, Any], _s: Settings) -> None:
        sent.append(payload)

    expired = (utcnow() - timedelta(seconds=1)).isoformat()
    await _enqueue(
        "send_email", {"to": "a@example.com", "subject": "123456", "expires_at": expired}
    )
    await process_due_jobs(get_database(), settings, {"send_email": send})
    [job] = await _jobs()
    assert sent == []
    assert job.status == JobStatus.FAILED and job.payload == {}
    assert "expired" in (job.last_error or "")


async def test_worker_wipes_payload_when_retry_would_be_after_expiry(settings):
    async def boom(_p: dict[str, Any], _s: Settings) -> None:
        raise RuntimeError("smtp down")

    soon = (utcnow() + timedelta(seconds=5)).isoformat()  # first retry is 15 s away
    await _enqueue("send_email", {"subject": "123456", "expires_at": soon}, max_attempts=5)
    await process_due_jobs(get_database(), settings, {"send_email": boom})
    [job] = await _jobs()
    assert job.status == JobStatus.FAILED and job.payload == {}


async def test_noop_email_jobs_are_handled(settings):
    assert NOOP_EMAIL_JOB in HANDLERS
    await _enqueue(NOOP_EMAIL_JOB, {"expires_at": (utcnow() + timedelta(minutes=5)).isoformat()})
    await process_due_jobs(get_database(), settings)
    [job] = await _jobs()
    assert job.status == JobStatus.DONE and job.payload == {}


async def test_argon2_runs_in_bounded_worker_threads(settings, monkeypatch):
    settings.password_hash_concurrency = 2
    loop_thread = threading.get_ident()
    state = {"active": 0, "max": 0, "threads": set()}
    lock = threading.Lock()

    def slow_hash(password: str) -> str:
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            state["threads"].add(threading.get_ident())
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
        return "hash:" + password

    monkeypatch.setattr(security, "hash_password", slow_hash)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        for _ in range(10):
            await asyncio.sleep(0.01)
            ticks += 1

    results = await asyncio.gather(
        *(security.hash_password_async(f"pw{i}") for i in range(6)), ticker()
    )
    assert results[:6] == [f"hash:pw{i}" for i in range(6)]
    assert state["max"] == 2  # bounded concurrency
    assert loop_thread not in state["threads"]  # never on the event-loop thread
    assert ticks == 10  # the loop kept running meanwhile
