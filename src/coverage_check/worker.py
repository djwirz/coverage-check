"""Workers: claim, call the payer, record. Run with: coverage-check worker

Nothing here holds state that matters. A worker can die at any point: its leases and slots
expire, and the same idempotency key means the payer isn't billed again for an answer it
already gave.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from coverage_check import limiter, queue
from coverage_check.config import WorkerSettings
from coverage_check.payer_client import PayerClient
from coverage_check.policy import Failed, GiveUp, Retry, backoff_seconds, classify

log = logging.getLogger("coverage_check.worker")
DATABASE_RETRY_SECONDS = 2

Pool = AsyncConnectionPool[psycopg.AsyncConnection[Any]]


async def handle(
    pool: Pool, payer: PayerClient, settings: WorkerSettings, claimed: queue.Claim
) -> None:
    """One attempt."""
    ids = {
        "batch_id": claimed.batch_id,
        "check_id": claimed.check_id,
        "attempt_id": claimed.attempt_id,
        "attempt": claimed.attempts,
    }
    if time.monotonic() - claimed.granted_at > settings.grant_max_age_seconds:
        # Too old to send safely. Give the check back; this didn't use an attempt.
        async with pool.connection() as conn:
            await queue.finish(conn, claimed, Retry("stale_grant"), attempt_class=queue.NOT_SENT)
        log.warning("grant_expired", extra=ids)
        return

    started = time.monotonic()
    seen = await payer.ask(claimed.request, claimed.key)
    decision = classify(seen, claimed.malformed_before)
    attempt_class = decision.reason if isinstance(decision, Retry | GiveUp) else "answered"
    if attempt_class == "malformed_response":
        attempt_class = "malformed"
    delay = 0.0
    if isinstance(decision, Retry):
        if claimed.attempts >= settings.max_attempts:
            decision = GiveUp("payer_unavailable")
        else:
            delay = max(
                decision.min_delay_seconds,
                backoff_seconds(
                    claimed.attempts, settings.backoff_base_seconds, settings.backoff_cap_seconds
                ),
            )
    # If the payer may still be working on it, its slot runs out by itself.
    keep_slot = isinstance(seen, Failed) and seen.sent
    async with pool.connection() as conn:
        held = await queue.finish(
            conn,
            claimed,
            decision,
            attempt_class=attempt_class,
            http_status=None if isinstance(seen, Failed) else seen.status,
            # Kept only when someone may need to look at what came back.
            body=seen.body
            if not isinstance(seen, Failed) and attempt_class in ("malformed", "unexpected_status")
            else None,
            delay_seconds=delay,
            release_slot=not keep_slot,
        )
    log.info(
        "attempt" if held else "late_reply",
        extra={
            **ids,
            "outcome": attempt_class,
            "decision": type(decision).__name__,
            "seconds": round(time.monotonic() - started, 3),
        },
    )


async def _loop(
    pool: Pool, payer: PayerClient, settings: WorkerSettings, stop: asyncio.Event
) -> None:
    async def pause(seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), seconds)

    while not stop.is_set():
        try:
            async with pool.connection() as conn:
                claimed = await queue.claim(conn, settings)
            match claimed:
                case queue.Claim():
                    await handle(pool, payer, settings, claimed)
                case queue.Wait(seconds):
                    # A little jitter, so waiting workers don't all come back at once.
                    await pause(seconds + random.uniform(0, 0.2 / settings.rate_per_second))  # noqa: S311
                case queue.Idle():
                    await pause(settings.idle_seconds * random.uniform(0.5, 1.5))  # noqa: S311
                case queue.Exhausted(check_id):
                    log.info(
                        "gave_up", extra={"check_id": check_id, "outcome": "payer_unavailable"}
                    )
        except (psycopg.OperationalError, PoolTimeout):
            log.warning("database_unavailable", exc_info=True)
            await pause(DATABASE_RETRY_SECONDS)
        except Exception:
            # Whatever was claimed comes back when its lease expires.
            log.exception("worker_error")
            await pause(DATABASE_RETRY_SECONDS)


async def run(settings: WorkerSettings, stop: asyncio.Event) -> None:
    """Work until `stop` is set. Each task finishes the attempt it is on first."""
    payer = PayerClient(settings.payer_url, settings.attempt_timeout_seconds)
    try:
        async with AsyncConnectionPool(
            settings.database_url, min_size=1, max_size=settings.concurrency, open=False
        ) as pool:
            await pool.wait()
            async with pool.connection() as conn:
                await limiter.configure(conn, settings)
            log.info("worker_started", extra={"concurrency": settings.concurrency})
            async with asyncio.TaskGroup() as tasks:
                for _ in range(settings.concurrency):
                    tasks.create_task(_loop(pool, payer, settings, stop))
    finally:
        await payer.close()
