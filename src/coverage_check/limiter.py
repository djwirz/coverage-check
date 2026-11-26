"""The shared token bucket and in-flight cap, in Postgres (ADR 0005).

Time is the database's clock_timestamp(), read after the row lock is held. now() would be
the start of the transaction, which is stale after waiting for the lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection

from coverage_check.config import WorkerSettings


def refill(tokens: float, burst: int, rate: float, elapsed_seconds: float) -> float:
    return min(float(burst), tokens + max(0.0, elapsed_seconds) * rate)


async def configure(conn: AsyncConnection[Any], settings: WorkerSettings) -> None:
    """Create the payer's bucket, or bring it up to the current limits."""
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO limiter_buckets AS b"
            " (payer_api, rate_per_second, burst, tokens, refilled_at, max_in_flight)"
            " VALUES (%(api)s, %(rate)s, %(burst)s, 0, clock_timestamp(), %(cap)s)"
            " ON CONFLICT (payer_api) DO UPDATE SET rate_per_second = %(rate)s,"
            " burst = %(burst)s, max_in_flight = %(cap)s,"
            " tokens = LEAST(b.tokens, %(burst)s)",
            {
                "api": settings.payer_api,
                "rate": settings.rate_per_second,
                "burst": settings.burst,
                "cap": settings.max_in_flight,
            },
        )


@dataclass(frozen=True)
class Bucket:
    """A locked bucket row, refilled up to now."""

    payer_api: str
    rate: float
    tokens: float
    max_in_flight: int
    now: datetime


async def lock(conn: AsyncConnection[Any], payer_api: str) -> Bucket:
    """Lock the bucket until the transaction ends. Claims pass through here one at a time."""
    cursor = await conn.execute(
        "SELECT rate_per_second, burst, tokens, refilled_at, max_in_flight"
        " FROM limiter_buckets WHERE payer_api = %s FOR UPDATE",
        (payer_api,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise LookupError(f"no limiter bucket for {payer_api}")
    rate, burst, tokens, refilled_at, cap = row
    cursor = await conn.execute("SELECT clock_timestamp()")
    clock = await cursor.fetchone()
    assert clock is not None  # noqa: S101 - always one row
    elapsed = (clock[0] - refilled_at).total_seconds()
    return Bucket(
        payer_api=payer_api,
        rate=float(rate),
        tokens=refill(float(tokens), burst, float(rate), elapsed),
        max_in_flight=cap,
        now=clock[0],
    )


async def in_flight(conn: AsyncConnection[Any], bucket: Bucket) -> int:
    """Slots in use. Expired ones, left by dead or hung callers, are dropped first."""
    await conn.execute(
        "DELETE FROM limiter_slots WHERE payer_api = %s AND expires_at <= %s",
        (bucket.payer_api, bucket.now),
    )
    cursor = await conn.execute(
        "SELECT count(*) FROM limiter_slots WHERE payer_api = %s", (bucket.payer_api,)
    )
    row = await cursor.fetchone()
    assert row is not None  # noqa: S101 - count always returns a row
    return int(row[0])
