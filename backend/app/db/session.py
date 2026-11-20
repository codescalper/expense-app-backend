"""Async engine / session management.

Services own transaction boundaries: they call ``await session.commit()`` once a unit of
work is complete. The request-scoped session rolls back anything left uncommitted.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings, normalize_async_db_url


def build_engine(settings: Settings, url: str | None = None) -> AsyncEngine:
    db_url, connect_args = normalize_async_db_url(url or settings.database_url)
    kwargs: dict[str, Any] = {
        "echo": settings.db_echo,
        "pool_pre_ping": True,
        # Never put bound parameters (password hashes, token hashes, emails) into exception
        # messages or SQL echo logs.
        "hide_parameters": True,
    }
    if settings.db_pgbouncer:
        # Transaction-mode poolers can't keep named prepared statements per connection.
        connect_args["statement_cache_size"] = 0
        connect_args["prepared_statement_name_func"] = lambda: f"__asyncpg_{uuid.uuid4()}__"
    if settings.environment == "test":
        kwargs["poolclass"] = NullPool
    else:
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
    return create_async_engine(db_url, connect_args=connect_args, **kwargs)


class Database:
    def __init__(self, settings: Settings) -> None:
        self.engine = build_engine(settings)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def dispose(self) -> None:
        await self.engine.dispose()


_database: Database | None = None


def get_database() -> Database:
    global _database
    if _database is None:
        _database = Database(get_settings())
    return _database


async def close_database() -> None:
    global _database
    if _database is not None:
        await _database.dispose()
        _database = None


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one AsyncSession per request."""
    async with get_database().sessionmaker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
