"""Run CPU-heavy work (Argon2, image decoding) off the event loop with bounded concurrency.

``anyio.CapacityLimiter`` objects belong to one event loop, so one limiter per (name, loop) is
created lazily. Production has a single loop per process; tests create a loop per test.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable

import anyio
import anyio.to_thread

_limiters: dict[str, weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, anyio.CapacityLimiter]]
_limiters = {}


def _limiter(name: str, capacity: int) -> anyio.CapacityLimiter:
    loop = asyncio.get_running_loop()
    per_loop = _limiters.setdefault(name, weakref.WeakKeyDictionary())
    limiter = per_loop.get(loop)
    if limiter is None:
        limiter = anyio.CapacityLimiter(capacity)
        per_loop[loop] = limiter
    elif limiter.total_tokens != capacity:
        limiter.total_tokens = capacity
    return limiter


async def run_limited[T](name: str, capacity: int, func: Callable[..., T], *args: object) -> T:
    """Run ``func(*args)`` in a worker thread; at most ``capacity`` run at once per ``name``."""
    return await anyio.to_thread.run_sync(func, *args, limiter=_limiter(name, capacity))
