"""Test fixtures. Tests run against a real PostgreSQL database (TEST_DATABASE_URL), never mocks.

The schema is built by running the real Alembic migrations (downgrade to base, then upgrade),
so every test run also proves the migrations round-trip.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import threading
from collections.abc import AsyncIterator, Iterator
from typing import Any

# --- environment must be set before the app is imported -------------------------------
_TEST_DB = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/family_budget_test",
)
if "test" not in _TEST_DB.rsplit("/", 1)[-1]:
    raise RuntimeError(f"Refusing to run tests against a non-test database: {_TEST_DB}")
os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DATABASE_URL": _TEST_DB,
        "DATABASE_MIGRATION_URL": _TEST_DB,
        "DB_PGBOUNCER": "false",
        "JWT_SECRET_KEY": "test-signing-key-" + "k" * 40,
        "ARGON2_TIME_COST": "1",
        "ARGON2_MEMORY_KIB": "1024",
        "ARGON2_PARALLELISM": "1",
        "RATE_LIMIT_ENABLED": "true",
        "UPLOAD_DIR": tempfile.mkdtemp(prefix="fb-test-uploads-"),
        "CORS_ORIGINS": "http://localhost:8081",
    }
)

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402

import app.models  # noqa: E402,F401 - registers every table
from app.core.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import close_database, get_database  # noqa: E402

# Every mapped table, so new feature tables are cleaned automatically between tests.
TABLES = ", ".join(t.name for t in Base.metadata.sorted_tables)
PASSWORD = "correct horse battery staple"


def _load_builtin_categories() -> list[dict[str, Any]]:
    """Built-in categories exactly as migration 0002 seeds them (single source of truth)."""
    import importlib.util
    from pathlib import Path

    path = next((Path(__file__).resolve().parents[1] / "alembic" / "versions").glob("*_0002_*.py"))
    spec = importlib.util.spec_from_file_location("_migration_0002", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [
        {"id": id_, "kind": kind, "name": name, "icon": icon, "color": color, "sort_order": order}
        for id_, kind, name, icon, color, order in module.DEFAULT_CATEGORIES
    ]


BUILTIN_CATEGORIES = _load_builtin_categories()


class _ExclusiveRunLock:
    """A PostgreSQL session-level advisory lock on the test database, held for the whole pytest
    run by a helper thread (with its own event loop and connection).

    Two runs against the same database would otherwise wreck each other: each run drops and
    re-creates the schema at start-up and truncates every table around every test, and the
    worker-style code under test (UPI reminders, the push sender, notification checks) scans
    whole tables. That interference — not the tests themselves — produced the intermittent
    failures that disappeared when a test ran alone. A second run now waits for the first
    (and says so) instead of corrupting it; give each concurrent run its own TEST_DATABASE_URL.
    """

    KEY = 0x46_42_54_45_53_54  # "FBTEST"
    TIMEOUT_SECONDS = 30 * 60

    def __init__(self, url: str) -> None:
        self.dsn = url.replace("postgresql+asyncpg://", "postgresql://", 1)
        self.acquired = threading.Event()
        self.release_requested = threading.Event()
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name="test-db-lock", daemon=True)

    def _run(self) -> None:
        try:
            asyncio.run(self._hold())
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            self.error = exc
            self.acquired.set()

    async def _hold(self) -> None:
        conn = await asyncpg.connect(self.dsn)
        try:
            waited = 0.0
            while not await conn.fetchval("SELECT pg_try_advisory_lock($1)", self.KEY):
                if waited == 0:
                    print(
                        f"\n[tests] another test run is using {self.dsn.rsplit('/', 1)[-1]}; "
                        "waiting for it (set TEST_DATABASE_URL to a database of your own)…",
                        file=sys.stderr,
                        flush=True,
                    )
                if waited >= self.TIMEOUT_SECONDS:
                    raise RuntimeError("Timed out waiting for the test database lock")
                await asyncio.sleep(2)
                waited += 2
            self.acquired.set()
            await asyncio.get_running_loop().run_in_executor(None, self.release_requested.wait)
        finally:
            await conn.close()

    def __enter__(self) -> _ExclusiveRunLock:
        self.thread.start()
        self.acquired.wait()
        if self.error is not None:
            raise RuntimeError("Could not lock the test database") from self.error
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release_requested.set()
        self.thread.join(timeout=30)


@pytest.fixture(scope="session", autouse=True)
def _migrated_database() -> Iterator[None]:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(here, "alembic.ini"))
    cfg.attributes["database_url"] = _TEST_DB
    with _ExclusiveRunLock(_TEST_DB):
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
        yield


@pytest.fixture(autouse=True)
async def _clean_database() -> AsyncIterator[None]:
    async with get_database().engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {TABLES} CASCADE"))
        # TRUNCATE also wipes reference data seeded by migrations; restore it.
        await conn.execute(
            text(
                "INSERT INTO transaction_categories "
                "(id, kind, name, icon, color, sort_order) "
                "VALUES (CAST(:id AS uuid), CAST(:kind AS category_kind), "
                ":name, :icon, :color, :sort_order)"
            ),
            BUILTIN_CATEGORIES,
        )
    yield
    # Also clean up afterwards so a test DB is never left with rows referencing seed data.
    async with get_database().engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    await close_database()


@pytest.fixture
def settings():
    s = get_settings()
    snapshot = s.model_dump()
    yield s
    for key, value in snapshot.items():  # undo per-test tweaks
        object.__setattr__(s, key, value)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    from app.main import app

    transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --- helpers ------------------------------------------------------------------------------


async def emails_to(address: str) -> list[dict[str, Any]]:
    """Queued outgoing emails (the send_email job payloads), newest first."""
    async with get_database().sessionmaker() as s:
        rows = await s.execute(
            text(
                "SELECT payload FROM background_jobs WHERE kind = 'send_email' "
                "AND payload->>'to' = :to ORDER BY created_at DESC"
            ),
            {"to": address},
        )
        return [r[0] for r in rows]


async def latest_code(address: str) -> str:
    mails = await emails_to(address)
    assert mails, f"no email queued for {address}"
    match = re.search(r"\b(\d{6})\b", mails[0]["subject"])
    assert match
    return match.group(1)


def bearer(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def register_verified(
    client: httpx.AsyncClient,
    email: str = "asha@example.com",
    password: str = PASSWORD,
    full_name: str = "Asha Rao",
) -> dict[str, Any]:
    """Registers + verifies a user. Returns the AuthResponse JSON (user + tokens)."""
    r = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": password, "full_name": full_name}
    )
    assert r.status_code == 202, r.text
    code = await latest_code(email.lower())
    r = await client.post(
        "/api/v1/auth/verify-email", json={"email": email, "code": code, "device_name": "Pixel"}
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


async def login(
    client: httpx.AsyncClient, email: str = "asha@example.com", password: str = PASSWORD
) -> httpx.Response:
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})
