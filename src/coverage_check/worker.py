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
from pathlib import Path
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from coverage_check import limiter, queue
from coverage_check.config import WorkerSettings
from coverage_check.payer_client import PayerClient
from coverage_check.policy import Failed, GiveUp, Retry, backoff_seconds, classify

log = logging.getLogger("coverage_check.worker")
DATABASE_RETRY_SECONDS = 2
# How long to wait for a connection before saying the database is unavailable.
DATABASE_WAIT_SECONDS = 5
HEARTBEAT_SECONDS = 5

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
    try:
        seen = await payer.ask(claimed.request, claimed.key)
    except asyncio.CancelledError:
        # Out of grace. Hand the check back now and don't make it wait for its lease. The
        # payer may have the request, so the slot stays; the same key makes asking again safe.
        async with pool.connection() as conn:
            await queue.finish(
                conn,
                claimed,
                Retry("released"),
                attempt_class=queue.RELEASED,
                release_slot=False,
            )
        log.warning("released", extra=ids)
        raise
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


async def heartbeat(pool: Pool, directory: Path, seconds: float = HEARTBEAT_SECONDS) -> None:
    """For Kubernetes probes, which can't ask a worker anything: it serves no HTTP.

    `alive` is touched while the event loop turns, `ready` only while the database answers.
    A database outage makes workers unready; it must not get them restarted.
    """
    directory.mkdir(parents=True, exist_ok=True)
    while True:
        (directory / "alive").touch()
        try:
            async with pool.connection(timeout=seconds) as conn:
                await conn.execute("SELECT 1")
        except (psycopg.OperationalError, PoolTimeout):
            (directory / "ready").unlink(missing_ok=True)
        else:
            (directory / "ready").touch()
        await asyncio.sleep(seconds)


async def configure(pool: Pool, settings: WorkerSettings, stop: asyncio.Event) -> None:
    """Set up the payer's bucket, waiting while the database or its schema isn't there yet."""
    while not stop.is_set():
        try:
            async with pool.connection() as conn:
                await limiter.configure(conn, settings)
            return
        except (psycopg.OperationalError, PoolTimeout, psycopg.errors.UndefinedTable):
            log.warning("database_not_ready", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), DATABASE_RETRY_SECONDS)


async def run(settings: WorkerSettings, stop: asyncio.Event) -> None:
    """Work until `stop` is set, then finish what is in flight, within the grace period."""
    payer = PayerClient(settings.payer_url, settings.attempt_timeout_seconds)
    try:
        async with AsyncConnectionPool(
            settings.database_url,
            min_size=1,
            # One more than the tasks, so the heartbeat never waits behind them.
            max_size=settings.concurrency + 1,
            open=False,
            timeout=DATABASE_WAIT_SECONDS,
            check=AsyncConnectionPool.check_connection,
        ) as pool:
            beating = (
                asyncio.create_task(heartbeat(pool, Path(settings.health_dir)))
                if settings.health_dir
                else None
            )
            await configure(pool, settings, stop)
            log.info("worker_started", extra={"concurrency": settings.concurrency})
            tasks = [
                asyncio.create_task(_loop(pool, payer, settings, stop))
                for _ in range(settings.concurrency)
            ]
            try:
                await stop.wait()
                log.info("worker_stopping")
                _, late = await asyncio.wait(tasks, timeout=settings.shutdown_grace_seconds)
                for task in late:
                    task.cancel()
            except asyncio.CancelledError:
                for task in tasks:
                    task.cancel()
                raise
            finally:
                # Let cancelled tasks hand their checks back while the pool is still open.
                for outcome in await asyncio.gather(*tasks, return_exceptions=True):
                    if isinstance(outcome, Exception):
                        log.error("task_failed", extra={"error": type(outcome).__name__})
                if beating is not None:
                    beating.cancel()
                    await asyncio.gather(beating, return_exceptions=True)
            log.info("worker_stopped")
    finally:
        await payer.close()
