"""Serverless (Vercel + Neon) support: settings, engine, client IP, inline email delivery, the
cron tick endpoint, Vercel Blob storage and the deployment packaging."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosmtplib
import httpx
import pytest
from PIL import Image
from pydantic import SecretStr, ValidationError
from sqlalchemy import select, text
from sqlalchemy.pool import NullPool
from starlette.requests import Request

from app.core.config import Settings
from app.core.logging import configure_logging
from app.core.rate_limit import client_ip
from app.core.security import utcnow
from app.db.session import build_engine, get_database
from app.models.system import BackgroundJob, JobStatus, PeriodicTaskRun
from app.services.blob_storage import VercelBlobStorage, store_id_from_token
from app.services.email import NOOP_EMAIL_JOB, SEND_EMAIL_JOB
from app.services.s3_storage import EMPTY_SHA256, S3Storage, sigv4_headers
from app.services.storage import StorageError
from app.workers.cron import CRON_LOCK_KEY, is_due, run_tick
from app.workers.jobs import PeriodicTask
from app.workers.worker import PERIODIC, process_due_jobs
from tests.conftest import PASSWORD, bearer, login, register_verified
from tests.helpers_catalog import API, insert_tx, user
from tests.test_attachments import jpeg_with_exif, upload

BACKEND = Path(__file__).resolve().parents[1]
CRON_SECRET = "c" * 16 + "0123456789abcdef"
STORE = "abc123store"
BLOB_TOKEN = f"vercel_blob_rw_{STORE}_S3cretPart_with_underscores"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "postgresql://u:p@localhost/db_test",
        "jwt_secret_key": "a-real-looking-signing-key-" + "x" * 30,
        "environment": "development",
    }
    return Settings(**{**base, **overrides})


# --- settings ------------------------------------------------------------------------------


S3_SETTINGS: dict[str, Any] = {
    "storage_backend": "s3",
    "s3_endpoint_url": "https://br-example.storage.us-east-2.aws.neon.tech",
    "s3_access_key_id": "AKIDEXAMPLE",
    "s3_secret_access_key": "s3-secret-example-value",
    "s3_region": "us-east-2",
    "s3_bucket": "family-budget-uploads",
}


def test_s3_backend_requires_its_settings():
    with pytest.raises(ValidationError) as exc:
        _settings(storage_backend="s3")
    for name in ("S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_BUCKET"):
        assert name in str(exc.value)
    assert _settings(**S3_SETTINGS).s3_bucket == "family-budget-uploads"
    with pytest.raises(ValidationError, match="https"):
        _settings(
            **{**S3_SETTINGS, "s3_endpoint_url": "http://insecure.example"},
            environment="production",
        )


def test_blob_backend_requires_token():
    with pytest.raises(ValidationError, match="BLOB_READ_WRITE_TOKEN"):
        _settings(storage_backend="vercel_blob")
    assert _settings(storage_backend="vercel_blob", blob_read_write_token=BLOB_TOKEN)


@pytest.mark.parametrize("secret", ["short", "has spaces in it 1234567", "x" * 15, "ä" * 20])
def test_cron_secret_must_be_long_and_header_safe(secret):
    with pytest.raises(ValidationError, match="CRON_SECRET"):
        _settings(cron_secret=secret)


def test_production_on_vercel_requires_serverless_settings():
    with pytest.raises(ValidationError) as exc:
        _settings(environment="production", vercel=True)
    message = str(exc.value)
    for required in (
        "SERVERLESS=true",
        "EMAIL_DELIVERY=inline",
        "STORAGE_BACKEND=s3 or vercel_blob",
        "CRON_SECRET",
    ):
        assert required in message
    ok = _settings(
        environment="production",
        vercel=True,
        serverless=True,
        email_delivery="inline",
        storage_backend="vercel_blob",
        blob_read_write_token=BLOB_TOKEN,
        cron_secret=CRON_SECRET,
        db_pgbouncer=True,
        trust_vercel_proxy=True,
        max_receipt_bytes=4 * 1024 * 1024,
    )
    assert ok.deployment_warnings() == []
    with_s3 = _settings(
        environment="production",
        vercel=True,
        serverless=True,
        email_delivery="inline",
        cron_secret=CRON_SECRET,
        **S3_SETTINGS,
    )
    assert with_s3.storage_backend == "s3"
    # Not on Vercel (docker compose / self-hosted production): nothing new is required.
    assert _settings(environment="production").storage_backend == "local"


def test_deployment_warnings():
    on_vercel = _settings(vercel=True).deployment_warnings()
    assert any("EMAIL_DELIVERY" in w for w in on_vercel)
    assert any("TRUST_VERCEL_PROXY" in w for w in on_vercel)
    assert any("MAX_RECEIPT_BYTES" in w for w in on_vercel)
    assert any("STORAGE_BACKEND=local" in w for w in on_vercel)
    assert any("CRON_SECRET" in w for w in on_vercel)
    spoofable = _settings(trust_vercel_proxy=True).deployment_warnings()
    assert any("spoof" in w for w in spoofable)
    assert any("DB_PGBOUNCER" in w for w in _settings(serverless=True).deployment_warnings())
    assert _settings().deployment_warnings() == []  # the docker-compose defaults are clean


async def test_serverless_engine_uses_no_pool(settings):
    serverless = build_engine(settings.model_copy(update={"environment": "production"}))
    assert not isinstance(serverless.pool, NullPool)  # self-hosted keeps its pool
    await serverless.dispose()
    engine = build_engine(
        settings.model_copy(update={"environment": "production", "serverless": True})
    )
    assert isinstance(engine.pool, NullPool)
    await engine.dispose()


# --- client IP behind Vercel's proxy --------------------------------------------------------


def _request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("10.0.0.1", 1234),
    }
    return Request(scope)


def test_client_ip_uses_vercel_headers_only_when_trusted(settings):
    spoofed = {"x-forwarded-for": "198.51.100.7, 10.1.1.1", "x-real-ip": "198.51.100.8"}
    settings.trust_vercel_proxy = False
    assert client_ip(_request(spoofed)) == "10.0.0.1"

    settings.trust_vercel_proxy = True
    assert client_ip(_request({"x-forwarded-for": "198.51.100.7, 10.1.1.1"})) == "198.51.100.7"
    assert client_ip(_request(spoofed)) == "198.51.100.8"  # x-real-ip before x-forwarded-for
    both = {**spoofed, "x-vercel-forwarded-for": "2001:db8::1"}
    assert client_ip(_request(both)) == "2001:db8::1"
    assert client_ip(_request({"x-real-ip": "not-an-ip"})) == "10.0.0.1"
    assert client_ip(_request({})) == "10.0.0.1"


async def test_sessions_record_the_vercel_client_ip(client, settings):
    await register_verified(client)
    settings.trust_vercel_proxy = True
    r = await client.post(
        f"{API}/auth/login",
        json={"email": "asha@example.com", "password": PASSWORD},
        headers={"x-real-ip": "198.51.100.23"},
    )
    assert r.status_code == 200, r.text
    sessions = await client.get(f"{API}/users/me/sessions", headers=bearer(r.json()["tokens"]))
    current = [s for s in sessions.json() if s["is_current"]]
    assert current[0]["ip_address"] == "198.51.100.23"


# --- email delivery modes ------------------------------------------------------------------


@pytest.fixture
def smtp(monkeypatch) -> list[Any]:
    sent: list[Any] = []

    async def fake_send(message: Any, **_kwargs: Any) -> None:
        sent.append(message)

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    return sent


async def _jobs(kind: str | None = None) -> list[BackgroundJob]:
    async with get_database().sessionmaker() as s:
        query = select(BackgroundJob).order_by(BackgroundJob.created_at)
        if kind is not None:
            query = query.where(BackgroundJob.kind == kind)
        return list(await s.scalars(query))


async def _register(client: httpx.AsyncClient, email: str = "asha@example.com") -> None:
    r = await client.post(
        f"{API}/auth/register",
        json={"email": email, "password": PASSWORD, "full_name": "Asha Rao"},
    )
    assert r.status_code == 202, r.text


async def test_queue_mode_only_enqueues(client, settings, smtp):
    assert settings.email_delivery == "queue"
    await _register(client)
    assert smtp == []
    [job] = await _jobs(SEND_EMAIL_JOB)
    assert job.status == JobStatus.PENDING and job.payload["to"] == "asha@example.com"
    await process_due_jobs(get_database(), settings)
    assert [m["To"] for m in smtp] == ["asha@example.com"]


async def test_inline_mode_sends_after_commit_and_wipes_payload(client, settings, smtp, caplog):
    settings.email_delivery = "inline"
    await _register(client)
    assert len(smtp) == 1 and smtp[0]["To"] == "asha@example.com"
    code = re.search(r"\b(\d{6})\b", smtp[0]["Subject"])
    assert code is not None
    [job] = await _jobs(SEND_EMAIL_JOB)
    assert job.status == JobStatus.DONE and job.payload == {}  # the code is gone from the DB
    assert code.group(1) not in caplog.text
    # The code works, and the queue doesn't send the same email a second time.
    r = await client.post(
        f"{API}/auth/verify-email",
        json={"email": "asha@example.com", "code": code.group(1)},
    )
    assert r.status_code == 200, r.text
    await process_due_jobs(get_database(), settings)
    assert len(smtp) == 1


async def test_inline_mode_keeps_unknown_and_known_addresses_alike(client, settings, smtp):
    await register_verified(client)  # queued (queue mode); drained below
    await process_due_jobs(get_database(), settings)
    smtp.clear()
    async with get_database().engine.begin() as conn:
        await conn.execute(text("DELETE FROM background_jobs"))

    settings.email_delivery = "inline"
    known = await client.post(f"{API}/auth/forgot-password", json={"email": "asha@example.com"})
    unknown = await client.post(f"{API}/auth/forgot-password", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert [m["To"] for m in smtp] == ["asha@example.com"]  # nothing sent to unknown addresses
    jobs = await _jobs()
    # One job per request either way, both run to completion, no payload left behind.
    assert sorted(j.kind for j in jobs) == sorted([SEND_EMAIL_JOB, NOOP_EMAIL_JOB])
    assert all(j.status == JobStatus.DONE and j.payload == {} for j in jobs)


async def test_inline_send_failure_stays_queued_for_retry(client, settings, monkeypatch):
    settings.email_delivery = "inline"

    async def down(message: Any, **_kwargs: Any) -> None:
        raise aiosmtplib.SMTPException(f"relay refused {message['To']}")

    monkeypatch.setattr(aiosmtplib, "send", down)
    await _register(client)  # the request itself still succeeds
    [job] = await _jobs(SEND_EMAIL_JOB)
    assert job.status == JobStatus.PENDING and job.attempts == 1
    assert job.run_at > utcnow() and job.payload["to"] == "asha@example.com"
    assert "@" not in (job.last_error or "")  # addresses are scrubbed from errors


async def test_inline_mode_also_delivers_family_invitations(client, settings, smtp):
    owner = await user(client, "owner")
    r = await client.post(f"{API}/families", json={"name": "Rao Family"}, headers=owner.h)
    assert r.status_code == 201, r.text
    settings.email_delivery = "inline"
    r = await client.post(
        f"{API}/families/{r.json()['id']}/invitations",
        json={"email": "guest@example.com", "role": "member"},
        headers=owner.h,
    )
    assert r.status_code == 201, r.text
    assert [m["To"] for m in smtp] == ["guest@example.com"]


# --- cron tick endpoint --------------------------------------------------------------------


async def test_cron_tick_is_404_without_secret(client, settings):
    settings.cron_secret = None
    assert (await client.get("/internal/cron/tick")).status_code == 404
    r = await client.post("/internal/cron/tick", headers={"Authorization": "Bearer x"})
    assert r.status_code == 404


async def test_cron_tick_requires_the_bearer_secret(client, settings):
    settings.cron_secret = SecretStr(CRON_SECRET)
    for headers in (
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": CRON_SECRET},
        {"Authorization": f"Basic {CRON_SECRET}"},
        {"Authorization": f"Bearer {CRON_SECRET}x"},
    ):
        r = await client.get("/internal/cron/tick", headers=headers)
        assert r.status_code == 401, headers
        assert r.json()["error"]["code"] == "invalid_cron_secret"
    async with get_database().sessionmaker() as s:
        assert await s.scalar(select(PeriodicTaskRun)) is None  # nothing ran


async def test_cron_tick_runs_everything_once_then_is_idempotent(client, settings, smtp):
    settings.cron_secret = SecretStr(CRON_SECRET)
    auth = {"Authorization": f"Bearer {CRON_SECRET}"}
    await _register(client)  # queue mode: a pending send_email job

    r = await client.get("/internal/cron/tick", headers=auth)  # Vercel Cron uses GET
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["status"] == "ok" and body["failed"] == [] and body["jobs_processed"] == 1
    assert set(body["ran"]) == {"housekeeping", *(t.name for t in PERIODIC)}
    assert [m["To"] for m in smtp] == ["asha@example.com"]

    again = await client.post("/internal/cron/tick", headers=auth)  # pingers may POST
    assert again.status_code == 200
    assert again.json()["ran"] == [] and again.json()["jobs_processed"] == 0
    assert len(smtp) == 1
    async with get_database().sessionmaker() as s:
        runs = {r.name: r.last_status for r in await s.scalars(select(PeriodicTaskRun))}
    assert set(runs) == set(body["ran"]) and set(runs.values()) == {"ok"}


def _counting_task(name: str, every: timedelta, calls: list[str]) -> PeriodicTask:
    async def run(_db: Any, _settings: Settings) -> None:
        calls.append(name)

    return PeriodicTask(name=name, every=every, run=run)


async def _age_run(name: str, by: timedelta) -> None:
    async with get_database().engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE periodic_task_runs "
                "SET last_run_at = last_run_at - make_interval(secs => :secs) WHERE name = :n"
            ),
            {"secs": by.total_seconds(), "n": name},
        )


async def test_periodic_intervals_are_respected(settings):
    calls: list[str] = []
    hourly = _counting_task("t_hourly", timedelta(hours=1), calls)
    fast = _counting_task("t_fast", timedelta(seconds=10), calls)
    db = get_database()

    first = await run_tick(db, settings, tasks=[hourly, fast], handlers={})
    assert first.ran == ["t_hourly", "t_fast"] and calls == ["t_hourly", "t_fast"]
    assert (await run_tick(db, settings, tasks=[hourly, fast], handlers={})).ran == []

    await _age_run("t_fast", timedelta(seconds=6))  # 10 s interval, 5 s slack: due
    await _age_run("t_hourly", timedelta(minutes=10))  # 50 min left: not due
    assert (await run_tick(db, settings, tasks=[hourly, fast], handlers={})).ran == ["t_fast"]

    await _age_run("t_hourly", timedelta(minutes=50))
    assert (await run_tick(db, settings, tasks=[hourly, fast], handlers={})).ran == ["t_hourly"]
    assert calls == ["t_hourly", "t_fast", "t_fast", "t_hourly"]


def test_due_check_tolerates_cron_jitter():
    now = utcnow()
    five = timedelta(minutes=5)
    assert is_due(None, five, now)
    assert is_due(now - timedelta(minutes=4, seconds=1), five, now)  # tick arrived early
    assert not is_due(now - timedelta(minutes=3), five, now)
    assert not is_due(now - timedelta(seconds=4), timedelta(seconds=10), now)


async def test_failing_task_is_recorded_and_does_not_stop_others(settings):
    calls: list[str] = []

    async def boom(_db: Any, _settings: Settings) -> None:
        raise RuntimeError("upstream down")

    tasks = [
        PeriodicTask(name="t_boom", every=timedelta(minutes=5), run=boom),
        _counting_task("t_ok", timedelta(minutes=5), calls),
    ]
    result = await run_tick(get_database(), settings, tasks=tasks, handlers={})
    assert result.status == "ok" and result.failed == ["t_boom"] and result.ran == ["t_ok"]
    async with get_database().sessionmaker() as s:
        row = await s.get(PeriodicTaskRun, "t_boom")
    assert row is not None and row.last_status == "failed"


async def test_advisory_lock_prevents_overlapping_ticks(settings):
    calls: list[str] = []
    task = _counting_task("t_locked", timedelta(minutes=5), calls)
    async with get_database().engine.connect() as conn, conn.begin():
        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CRON_LOCK_KEY})
        result = await run_tick(get_database(), settings, tasks=[task], handlers={})
        assert result.status == "locked" and result.ran == [] and calls == []
    # Lock released (transaction ended): the next tick runs normally.
    assert (await run_tick(get_database(), settings, tasks=[task], handlers={})).ran == ["t_locked"]


async def test_concurrent_ticks_run_tasks_once(settings):
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def slow(_db: Any, _settings: Settings) -> None:
        calls.append("slow")
        started.set()
        await release.wait()

    task = PeriodicTask(name="t_slow", every=timedelta(minutes=5), run=slow)
    db = get_database()
    first = asyncio.create_task(run_tick(db, settings, tasks=[task], handlers={}))
    await asyncio.wait_for(started.wait(), 10)
    second = await run_tick(db, settings, tasks=[task], handlers={})
    release.set()
    assert second.status == "locked"
    assert (await first).ran == ["t_slow"] and calls == ["slow"]


async def test_tick_respects_the_time_budget(settings):
    calls: list[str] = []
    task = _counting_task("t_budget", timedelta(minutes=5), calls)
    result = await run_tick(get_database(), settings, tasks=[task], handlers={}, budget_seconds=1)
    assert result.deferred == ["t_budget"] and calls == []
    async with get_database().sessionmaker() as s:
        assert await s.get(PeriodicTaskRun, "t_budget") is None  # still due next time


# --- Vercel Blob storage -------------------------------------------------------------------


class FakeBlobStore:
    """Just enough of the Vercel Blob HTTP API, private store only."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.requests: list[httpx.Request] = []
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == f"Bearer {BLOB_TOKEN}"
        if self.down:
            return httpx.Response(503, json={"error": {"code": "service_unavailable"}})
        url = request.url
        if request.method == "PUT" and str(url).startswith("https://vercel.com/api/blob?"):
            pathname = url.params["pathname"]
            assert request.headers["x-vercel-blob-access"] == "private"
            assert request.headers["x-add-random-suffix"] == "0"
            assert request.headers["x-allow-overwrite"] == "0"
            assert request.headers["x-api-version"]
            if pathname in self.objects:
                return httpx.Response(400, json={"error": {"code": "bad_request"}})
            content_type = request.headers["x-content-type"]
            self.objects[pathname] = (request.content, content_type)
            blob_url = f"https://{STORE}.private.blob.vercel-storage.com/{pathname}"
            return httpx.Response(
                200,
                json={
                    "url": blob_url,
                    "downloadUrl": blob_url + "?download=1",
                    "pathname": pathname,
                    "contentType": content_type,
                    "contentDisposition": "inline",
                    "etag": '"1"',
                },
            )
        if request.method == "POST" and str(url) == "https://vercel.com/api/blob/delete":
            for blob_url in json.loads(request.content)["urls"]:
                self.objects.pop(httpx.URL(blob_url).path.lstrip("/"), None)
            return httpx.Response(200, json={})
        if request.method == "GET" and url.host == f"{STORE}.private.blob.vercel-storage.com":
            stored = self.objects.get(url.path.lstrip("/"))
            if stored is None:
                return httpx.Response(404)
            return httpx.Response(200, content=stored[0], headers={"content-type": stored[1]})
        return httpx.Response(500)


@pytest.fixture
def blob(settings, monkeypatch) -> FakeBlobStore:
    fake = FakeBlobStore()
    settings.storage_backend = "vercel_blob"
    settings.blob_read_write_token = SecretStr(BLOB_TOKEN)
    monkeypatch.setattr(VercelBlobStorage, "default_transport", httpx.MockTransport(fake.handler))
    return fake


def test_store_id_comes_from_the_token():
    assert store_id_from_token(BLOB_TOKEN) == STORE
    for bad in ("", "vercel_blob_rw_", "not-a-token", "vercel_blob_ro_abc_def"):
        with pytest.raises(ValueError):
            store_id_from_token(bad)


async def test_blob_storage_roundtrip():
    fake = FakeBlobStore()
    storage = VercelBlobStorage(BLOB_TOKEN, transport=httpx.MockTransport(fake.handler))
    key = await storage.save("receipts", b"%PDF-1.7 hello", "pdf")
    assert re.fullmatch(r"receipts/[A-Za-z0-9_-]{32}\.pdf", key)
    assert fake.objects[key] == (b"%PDF-1.7 hello", "application/pdf")

    stored = await storage.open(key)
    assert stored is not None and stored.chunks is not None and stored.close is not None
    assert b"".join([c async for c in stored.chunks]) == b"%PDF-1.7 hello"
    await stored.close()
    get = fake.requests[-1]
    assert str(get.url) == f"https://{STORE}.private.blob.vercel-storage.com/{key}"

    await storage.delete(key)
    assert key not in fake.objects
    assert await storage.open(key) is None
    await storage.delete(key)  # deleting twice is fine


async def test_blob_storage_errors_never_leak_the_token():
    fake = FakeBlobStore()
    fake.down = True
    storage = VercelBlobStorage(BLOB_TOKEN, transport=httpx.MockTransport(fake.handler))
    with pytest.raises(StorageError) as exc:
        await storage.save("avatars", b"x", "jpg")
    assert BLOB_TOKEN not in str(exc.value) and "S3cret" not in str(exc.value)

    def unreachable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    offline = VercelBlobStorage(BLOB_TOKEN, transport=httpx.MockTransport(unreachable))
    for call in (
        offline.save("avatars", b"x", "jpg"),
        offline.open("avatars/" + "a" * 32 + ".jpg"),
    ):
        with pytest.raises(StorageError):
            await call


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _no_blob_urls(response: httpx.Response) -> None:
    exposed = " ".join(f"{k}: {v}" for k, v in response.headers.items())
    assert "blob.vercel-storage.com" not in exposed
    assert b"blob.vercel-storage.com" not in response.content


async def test_avatar_lives_in_blob_and_is_served_through_the_api(client, blob):
    auth = await register_verified(client)
    h = bearer(auth["tokens"])
    r = await client.put(
        f"{API}/users/me/avatar", headers=h, files={"file": ("me.png", _png(), "image/png")}
    )
    assert r.status_code == 200, r.text
    _no_blob_urls(r)
    [key] = blob.objects
    assert key.startswith("avatars/") and blob.objects[key][1] == "image/jpeg"

    got = await client.get(f"{API}/users/me/avatar", headers=h)
    assert got.status_code == 200 and got.content == blob.objects[key][0]
    assert got.headers["content-type"] == "image/jpeg"
    assert got.headers["cache-control"] == "private, max-age=300"
    _no_blob_urls(got)
    assert (await client.get(f"{API}/users/me/avatar")).status_code == 401

    # Replacing the avatar deletes the old blob; removing it deletes the new one.
    r = await client.put(
        f"{API}/users/me/avatar", headers=h, files={"file": ("me.png", _png(), "image/png")}
    )
    assert r.status_code == 200 and len(blob.objects) == 1 and key not in blob.objects
    assert (await client.delete(f"{API}/users/me/avatar", headers=h)).status_code == 204
    assert blob.objects == {}
    assert (await client.get(f"{API}/users/me/avatar", headers=h)).status_code == 404


async def test_receipts_live_in_blob_and_download_with_auth(client, blob):
    asha = await user(client, "asha")
    tx = await insert_tx(asha)
    r = await upload(client, asha, tx, jpeg_with_exif((800, 600)))
    assert r.status_code == 201, r.text
    att = r.json()
    [key] = blob.objects
    assert key.startswith("receipts/")

    d = await client.get(att["download_url"], headers=asha.h)
    assert d.status_code == 200 and d.content == blob.objects[key][0]
    assert len(d.content) == att["size_bytes"]
    assert d.headers["content-type"] == "image/jpeg"
    assert d.headers["content-disposition"].startswith('attachment; filename="receipt-')
    assert d.headers["cache-control"] == "private, no-store"
    _no_blob_urls(d)
    assert (await client.get(att["download_url"])).status_code == 401
    bob = await user(client, "bob")
    assert (await client.get(att["download_url"], headers=bob.h)).status_code == 404


async def test_blob_outage_is_a_clean_503(client, blob):
    auth = await register_verified(client)
    blob.down = True
    r = await client.put(
        f"{API}/users/me/avatar",
        headers=bearer(auth["tokens"]),
        files={"file": ("me.png", _png(), "image/png")},
    )
    assert r.status_code == 503 and r.json()["error"]["code"] == "storage_unavailable"
    assert BLOB_TOKEN not in r.text
    me = await client.get(f"{API}/users/me", headers=bearer(auth["tokens"]))
    assert me.json()["has_avatar"] is False


async def test_family_image_lives_in_blob(client, blob):
    owner = await user(client, "owner")
    r = await client.post(f"{API}/families", json={"name": "Rao Family"}, headers=owner.h)
    family_id = r.json()["id"]
    r = await client.put(
        f"{API}/families/{family_id}/image",
        headers=owner.h,
        files={"file": ("f.png", _png(), "image/png")},
    )
    assert r.status_code == 200, r.text
    [key] = blob.objects
    assert key.startswith("families/")
    got = await client.get(f"{API}/families/{family_id}/image", headers=owner.h)
    assert got.status_code == 200 and got.content == blob.objects[key][0]


# --- S3-compatible storage (Neon object storage) --------------------------------------------


def test_sigv4_matches_the_aws_reference_examples():
    """The GET and PUT examples from the AWS "Signature Version 4 (header auth)" docs."""
    creds: dict[str, Any] = {
        "region": "us-east-1",
        "access_key_id": "AKIAIOSFODNN7EXAMPLE",
        "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "now": datetime(2013, 5, 24, tzinfo=UTC),
    }
    get = sigv4_headers(
        method="GET",
        url="https://examplebucket.s3.amazonaws.com/test.txt",
        payload_sha256=EMPTY_SHA256,
        headers={"Range": "bytes=0-9"},
        **creds,
    )
    assert get["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )
    put = sigv4_headers(
        method="PUT",
        url="https://examplebucket.s3.amazonaws.com/test%24file.text",
        payload_sha256="44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072",
        headers={
            "Date": "Fri, 24 May 2013 00:00:00 GMT",
            "x-amz-storage-class": "REDUCED_REDUNDANCY",
        },
        **creds,
    )
    assert put["authorization"].endswith(
        "Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
    )


class FakeS3:
    """Path-style S3 for one private bucket; re-checks every request's SigV4 signature."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.requests: list[httpx.Request] = []
        self.down = False

    def _check_signature(self, request: httpx.Request) -> None:
        signed_names = re.search(r"SignedHeaders=([^,]+)", request.headers["authorization"])
        assert signed_names is not None
        names = [n for n in signed_names.group(1).split(";") if n != "host"]
        stamp = datetime.strptime(request.headers["x-amz-date"], "%Y%m%dT%H%M%SZ")
        expected = sigv4_headers(
            method=request.method,
            url=str(request.url),
            region=S3_SETTINGS["s3_region"],
            access_key_id=S3_SETTINGS["s3_access_key_id"],
            secret_access_key=S3_SETTINGS["s3_secret_access_key"],
            payload_sha256=request.headers["x-amz-content-sha256"],
            headers={n: request.headers[n] for n in names if not n.startswith("x-amz-")},
            now=stamp.replace(tzinfo=UTC),
        )
        assert request.headers["authorization"] == expected["authorization"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self._check_signature(request)
        assert "x-amz-acl" not in request.headers  # never public
        if self.down:
            return httpx.Response(503, text="<Error><Code>SlowDown</Code></Error>")
        prefix = "/" + S3_SETTINGS["s3_bucket"] + "/"
        assert request.url.path.startswith(prefix)  # path-style addressing
        key = request.url.path[len(prefix) :]
        if request.method == "PUT":
            body = request.content
            assert hashlib.sha256(body).hexdigest() == request.headers["x-amz-content-sha256"]
            self.objects[key] = (body, request.headers["content-type"])
            return httpx.Response(200, headers={"etag": '"1"'})
        if request.method == "GET":
            stored = self.objects.get(key)
            if stored is None:
                return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")
            return httpx.Response(200, content=stored[0], headers={"content-type": stored[1]})
        if request.method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204)
        return httpx.Response(405)


@pytest.fixture
def s3(settings, monkeypatch) -> FakeS3:
    fake = FakeS3()
    for name, value in S3_SETTINGS.items():
        setattr(settings, name, SecretStr(value) if name == "s3_secret_access_key" else value)
    monkeypatch.setattr(S3Storage, "default_transport", httpx.MockTransport(fake.handler))
    return fake


def _s3_storage(fake: FakeS3) -> S3Storage:
    return S3Storage(
        endpoint_url=S3_SETTINGS["s3_endpoint_url"],
        bucket=S3_SETTINGS["s3_bucket"],
        region=S3_SETTINGS["s3_region"],
        access_key_id=S3_SETTINGS["s3_access_key_id"],
        secret_access_key=S3_SETTINGS["s3_secret_access_key"],
        transport=httpx.MockTransport(fake.handler),
    )


async def test_s3_storage_roundtrip():
    fake = FakeS3()
    storage = _s3_storage(fake)
    key = await storage.save("receipts", b"%PDF-1.7 hello", "pdf")
    assert re.fullmatch(r"receipts/[A-Za-z0-9_-]{32}\.pdf", key)
    assert fake.objects[key] == (b"%PDF-1.7 hello", "application/pdf")
    put = fake.requests[-1]
    assert str(put.url) == f"{S3_SETTINGS['s3_endpoint_url']}/family-budget-uploads/{key}"

    stored = await storage.open(key)
    assert stored is not None and stored.chunks is not None and stored.close is not None
    assert b"".join([c async for c in stored.chunks]) == b"%PDF-1.7 hello"
    await stored.close()

    await storage.delete(key)
    assert key not in fake.objects and await storage.open(key) is None
    await storage.delete(key)  # deleting twice is fine


async def test_s3_errors_never_leak_credentials():
    fake = FakeS3()
    fake.down = True
    storage = _s3_storage(fake)
    for call in (
        storage.save("avatars", b"x", "jpg"),
        storage.open("avatars/" + "a" * 32 + ".jpg"),
    ):
        with pytest.raises(StorageError) as exc:
            await call
        assert S3_SETTINGS["s3_secret_access_key"] not in str(exc.value)
        assert S3_SETTINGS["s3_access_key_id"] not in str(exc.value)


async def test_avatar_and_receipt_live_in_s3_behind_the_api(client, s3):
    auth = await register_verified(client)
    h = bearer(auth["tokens"])
    r = await client.put(
        f"{API}/users/me/avatar", headers=h, files={"file": ("me.png", _png(), "image/png")}
    )
    assert r.status_code == 200, r.text
    [avatar_key] = s3.objects
    assert avatar_key.startswith("avatars/")
    got = await client.get(f"{API}/users/me/avatar", headers=h)
    assert got.status_code == 200 and got.content == s3.objects[avatar_key][0]
    assert "neon.tech" not in " ".join(got.headers.values())
    assert (await client.get(f"{API}/users/me/avatar")).status_code == 401
    assert (await client.delete(f"{API}/users/me/avatar", headers=h)).status_code == 204
    assert s3.objects == {}

    ravi = await user(client, "ravi")
    tx = await insert_tx(ravi)
    r = await upload(client, ravi, tx, jpeg_with_exif((800, 600)))
    assert r.status_code == 201, r.text
    [receipt_key] = s3.objects
    d = await client.get(r.json()["download_url"], headers=ravi.h)
    assert d.status_code == 200 and d.content == s3.objects[receipt_key][0]
    assert d.headers["content-disposition"].startswith('attachment; filename="receipt-')
    assert "neon.tech" not in " ".join(d.headers.values())


async def test_s3_outage_is_a_clean_503(client, s3):
    auth = await register_verified(client)
    s3.down = True
    r = await client.put(
        f"{API}/users/me/avatar",
        headers=bearer(auth["tokens"]),
        files={"file": ("me.png", _png(), "image/png")},
    )
    assert r.status_code == 503 and r.json()["error"]["code"] == "storage_unavailable"
    assert S3_SETTINGS["s3_secret_access_key"] not in r.text


# --- health / docs in a production (Vercel) configuration -------------------------------------


async def test_health_works_and_docs_are_off_in_production(settings):
    from app.main import create_app

    settings.environment = "production"
    settings.serverless = True
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://test") as c:
            assert (await c.get("/health")).json() == {"status": "ok"}
            ready = await c.get("/health/ready")
            assert ready.status_code == 200 and ready.json()["database"] == "up"
            for path in ("/docs", "/redoc", "/openapi.json"):
                assert (await c.get(path)).status_code == 404
            assert "strict-transport-security" in (await c.get("/health")).headers
    finally:
        configure_logging("test")


# --- packaging: what Vercel reads -----------------------------------------------------------


def _requirement_name(spec: str) -> str:
    return re.split(r"[\[=<>~!]", spec, maxsplit=1)[0].strip().lower()


async def test_vercel_entrypoint_and_config_match_the_app(client, settings):
    pyproject = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    module, _, attr = pyproject["tool"]["vercel"]["entrypoint"].partition(":")
    assert (module, attr) == ("app.main", "app")
    vercel = json.loads((BACKEND / "vercel.json").read_text(encoding="utf-8"))
    function = vercel["functions"][module.replace(".", "/") + ".py"]
    assert 10 <= function["maxDuration"] <= 300  # Hobby's maximum
    assert "tests/**" in function["excludeFiles"]

    # The cron path is served by the app (401 without the bearer, not a 404 for a missing
    # route), for GET (what Vercel Cron sends) and POST (external pingers).
    settings.cron_secret = SecretStr(CRON_SECRET)
    for cron in vercel["crons"]:
        assert (await client.get(cron["path"])).status_code == 401
        assert (await client.post(cron["path"])).status_code == 401
        minute, hour, *_ = cron["schedule"].split()
        assert minute.isdigit() and hour.isdigit()  # at most daily: Hobby-safe


def test_pyproject_dependencies_mirror_requirements():
    """Vercel installs pyproject.toml; Docker installs requirements.txt. Keep them identical."""
    pins: dict[str, str] = {}
    for line in (BACKEND / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, _, version = line.partition("==")
            pins[name.lower()] = version
    top_level = [
        line.strip()
        for line in (BACKEND / "requirements.in").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    pyproject = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    expected = sorted(f"{spec}=={pins[_requirement_name(spec)]}" for spec in top_level)
    assert sorted(pyproject["project"]["dependencies"]) == expected
    constraints = pyproject["tool"]["uv"]["constraint-dependencies"]
    assert sorted(constraints) == sorted(f"{n}=={v}" for n, v in pins.items())
    assert pyproject["project"]["requires-python"] == ">=3.12,<3.13"


async def test_login_still_works_with_serverless_engine(client, settings):
    """Smoke: the whole request path on a NullPool engine (what SERVERLESS=true selects)."""
    await register_verified(client)
    assert (await login(client)).status_code == 200
