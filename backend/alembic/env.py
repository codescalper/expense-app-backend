"""Alembic environment (async). URL comes from settings, never from alembic.ini."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import build_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _migration_url() -> str:
    # Tests (and tooling) may inject a URL explicitly.
    override = config.attributes.get("database_url")
    if override:
        return str(override)
    settings = get_settings()
    return settings.database_migration_url or settings.database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_migration_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    settings = get_settings().model_copy(update={"db_pgbouncer": False})
    engine = build_engine(settings, url=_migration_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
