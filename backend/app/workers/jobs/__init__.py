"""Background job modules, discovered automatically by the worker.

Each module in this package may define:

* ``HANDLERS: dict[str, JobHandler]`` — job kind → coroutine ``(payload, settings) -> None``
  (enqueue with ``JobRepository(session).enqueue(kind, payload, dedupe_key=...)``).
* ``PERIODIC: list[PeriodicTask]`` — tasks the worker runs on an interval
  (e.g. generating recurring transactions, budget alerts, bill reminders).

Handlers must be idempotent: jobs are retried on failure and may run more than once.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.core.config import Settings
from app.db.session import Database

JobHandler = Callable[[dict[str, Any], Settings], Awaitable[None]]


@dataclass(frozen=True)
class PeriodicTask:
    name: str
    every: timedelta
    run: Callable[[Database, Settings], Awaitable[None]]


def discover() -> tuple[dict[str, JobHandler], list[PeriodicTask]]:
    handlers: dict[str, JobHandler] = {}
    periodic: list[PeriodicTask] = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{__name__}.{info.name}")
        for kind, handler in getattr(module, "HANDLERS", {}).items():
            if kind in handlers:
                raise RuntimeError(f"duplicate job handler for {kind!r} in {info.name}")
            handlers[kind] = handler
        periodic.extend(getattr(module, "PERIODIC", []))
    return handlers, periodic
