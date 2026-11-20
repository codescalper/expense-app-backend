"""Rate limiting, failure throttles and lockouts stored in PostgreSQL (no Redis).

Everything lives in ``rate_limit_counters`` rows ``(key, window_start, count, expires_at)``:

* fixed-window counters (``increment`` / ``hit``),
* decaying failure counters (``sliding_increment``: current window + the previous window
  weighted by how much of it still overlaps the sliding window),
* long-span counters summed over hourly buckets (``count_since``),
* locks (``lock`` / ``lock_remaining``): one row whose ``expires_at`` is the lock end.

Connection use: a ``RateLimiter()`` without a session opens its *own* short transaction, so the
counter persists even if the request later fails. Only use that mode while the request's own
session holds no connection (e.g. in a route dependency that runs before the handler touches
the DB), otherwise one request holds two pooled connections and a burst can starve the pool.
Inside services pass the request session (``RateLimiter(db)``): the counters then share the
request's transaction and connection, and the service must ``commit()`` before raising so the
failure is recorded (``hit`` does this itself before raising 429).
"""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import RateLimitedError
from app.core.security import keyed_hash
from app.db.session import get_database
from app.models.system import RateLimitCounter


def _now() -> datetime:
    """Current time for all limiter arithmetic (tests monkeypatch this to cross windows)."""
    return datetime.now(UTC)


def _window(now: datetime, window_seconds: int) -> tuple[datetime, datetime]:
    epoch = int(now.timestamp())
    start_epoch = epoch - (epoch % window_seconds)
    start = datetime.fromtimestamp(start_epoch, UTC)
    return start, start + timedelta(seconds=window_seconds)


def hashed_key(prefix: str, value: str) -> str:
    """Rate-limit keys never store raw emails: HMAC with a key derived from the server secret
    (a plain hash could be reversed by hashing a list of known addresses)."""
    return f"{prefix}:{keyed_hash(value.lower(), purpose='rate-limit')[:40]}"


def _seconds_until(end: datetime, now: datetime) -> int:
    return max(int((end - now).total_seconds()), 1)


class RateLimiter:
    def __init__(self, session: AsyncSession | None = None) -> None:
        self._db = session

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        if self._db is not None:
            yield self._db  # caller's transaction; the caller commits
            return
        async with get_database().sessionmaker() as session:
            yield session
            await session.commit()

    async def increment(
        self, key: str, window_seconds: int, *, retain_seconds: int = 0
    ) -> tuple[int, int]:
        """Atomically add one hit. Returns (count_in_window, seconds_until_reset)."""
        now = _now()
        start, end = _window(now, window_seconds)
        stmt = (
            pg_insert(RateLimitCounter)
            .values(
                key=key,
                window_start=start,
                count=1,
                expires_at=end + timedelta(seconds=retain_seconds),
            )
            .on_conflict_do_update(
                index_elements=[RateLimitCounter.key, RateLimitCounter.window_start],
                set_={"count": RateLimitCounter.count + 1},
            )
            .returning(RateLimitCounter.count)
        )
        async with self._session() as session:
            count = (await session.execute(stmt)).scalar_one()
        return int(count), _seconds_until(end, now)

    async def _count_at(self, session: AsyncSession, key: str, start: datetime) -> int:
        count = (
            await session.execute(
                select(func.coalesce(func.sum(RateLimitCounter.count), 0)).where(
                    RateLimitCounter.key == key, RateLimitCounter.window_start == start
                )
            )
        ).scalar_one()
        return int(count)

    async def current(self, key: str, window_seconds: int) -> tuple[int, int]:
        now = _now()
        start, end = _window(now, window_seconds)
        async with self._session() as session:
            count = await self._count_at(session, key, start)
        return count, _seconds_until(end, now)

    async def hit(self, key: str, limit: int, window_seconds: int) -> None:
        """Count a hit and raise 429 once the limit is exceeded."""
        count, retry_after = await self.increment(key, window_seconds)
        if count > limit:
            if self._db is not None:
                await self._db.commit()  # keep the hit even though the request fails
            raise RateLimitedError(retry_after)

    # --- decaying failure counters ------------------------------------------------------

    def _weight(self, now: datetime, window_seconds: int) -> tuple[datetime, datetime, float]:
        start, _ = _window(now, window_seconds)
        previous = start - timedelta(seconds=window_seconds)
        elapsed = (now - start).total_seconds()
        return start, previous, max(0.0, 1.0 - elapsed / window_seconds)

    async def sliding_count(self, key: str, window_seconds: int) -> float:
        now = _now()
        start, previous, weight = self._weight(now, window_seconds)
        async with self._session() as session:
            cur = await self._count_at(session, key, start)
            prev = await self._count_at(session, key, previous)
        return cur + prev * weight

    async def sliding_increment(self, key: str, window_seconds: int) -> float:
        """Add one failure; returns the decayed count over the last ``window_seconds``.
        Failures fade out completely after two windows without new ones."""
        now = _now()
        _, previous, weight = self._weight(now, window_seconds)
        cur, _ = await self.increment(key, window_seconds, retain_seconds=window_seconds)
        async with self._session() as session:
            prev = await self._count_at(session, key, previous)
        return cur + prev * weight

    # --- long-span counters -------------------------------------------------------------

    async def increment_bucketed(self, key: str, span_seconds: int, bucket_seconds: int) -> None:
        """Count one event in an hourly (``bucket_seconds``) bucket kept for ``span_seconds``."""
        await self.increment(key, bucket_seconds, retain_seconds=span_seconds)

    async def count_since(self, key: str, span_seconds: int) -> int:
        since = _now() - timedelta(seconds=span_seconds)
        async with self._session() as session:
            count = (
                await session.execute(
                    select(func.coalesce(func.sum(RateLimitCounter.count), 0)).where(
                        RateLimitCounter.key == key, RateLimitCounter.window_start >= since
                    )
                )
            ).scalar_one()
        return int(count)

    # --- locks --------------------------------------------------------------------------

    async def lock(self, key: str, seconds: int) -> None:
        now = _now()
        until = now + timedelta(seconds=seconds)
        stmt = (
            pg_insert(RateLimitCounter)
            .values(key=key, window_start=now, count=1, expires_at=until)
            .on_conflict_do_update(
                index_elements=[RateLimitCounter.key, RateLimitCounter.window_start],
                set_={"expires_at": func.greatest(RateLimitCounter.expires_at, until)},
            )
        )
        async with self._session() as session:
            await session.execute(stmt)

    async def lock_remaining(self, key: str) -> int:
        """Seconds until the lock on ``key`` lifts (0 = not locked)."""
        now = _now()
        async with self._session() as session:
            until = (
                await session.execute(
                    select(func.max(RateLimitCounter.expires_at)).where(
                        RateLimitCounter.key == key, RateLimitCounter.expires_at > now
                    )
                )
            ).scalar_one()
        return 0 if until is None else _seconds_until(until, now)

    async def reset(self, *keys: str) -> None:
        async with self._session() as session:
            await session.execute(delete(RateLimitCounter).where(RateLimitCounter.key.in_(keys)))


def client_ip(request: Request) -> str:
    # uvicorn --proxy-headers already rewrites request.client from trusted X-Forwarded-For.
    return request.client.host if request.client else "unknown"


def ip_limit_key(ip: str) -> str:
    """The unit an IP-based limit applies to: the address for IPv4, the /64 for IPv6.

    A single IPv6 subscriber usually controls a whole /64 (2^64 addresses), so limiting per
    address would be trivially bypassed by rotating addresses.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip[:64]
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.IPv6Network((addr, 64), strict=False))
    return str(addr)


def ip_rate_limit(scope: str) -> Callable[[Request], Awaitable[None]]:
    """FastAPI dependency factory: per-IP (IPv6: per-/64) limit for one endpoint scope.

    Runs before the handler touches its DB session, so its own short transaction never
    overlaps the request's connection."""

    async def _dependency(request: Request) -> None:
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return
        await RateLimiter().hit(
            f"ip:{scope}:{ip_limit_key(client_ip(request))}",
            settings.auth_ip_rate_limit,
            settings.auth_ip_rate_window_seconds,
        )

    return _dependency
