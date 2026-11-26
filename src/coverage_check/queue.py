"""Claiming checks and recording what happened to them (ADRs 0004 and 0010).

Every function is one short transaction. Rows are locked in the order check, attempt, slot,
so a claim and a finish can't deadlock each other.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from coverage_check import fhir, limiter
from coverage_check.config import WorkerSettings
from coverage_check.models import CheckRequest
from coverage_check.policy import Decision, Done, GiveUp, Retry

# Attempts that don't use up the cap: never sent, or handed back by a stopping worker.
NOT_SENT = "not_sent"
RELEASED = "released"
UNCOUNTED = [NOT_SENT, RELEASED]
MAX_KEPT_BODY = 2000


@dataclass(frozen=True)
class Claim:
    check_id: UUID
    batch_id: UUID
    attempt_id: UUID
    lease_id: UUID
    key: str  # the payer's idempotency key: the same for every attempt in a cycle
    request: CheckRequest
    attempts: int  # in this cycle, counting this one
    malformed_before: int
    granted_at: float  # time.monotonic() when the token was taken
    traceparent: str | None = None


@dataclass(frozen=True)
class Wait:
    """No capacity just now."""

    seconds: float


@dataclass(frozen=True)
class Idle:
    """Nothing is ready to be claimed."""


@dataclass(frozen=True)
class Exhausted:
    """A check was out of attempts, and became an exception without another call."""

    check_id: UUID
    traceparent: str | None = None


async def reap(conn: AsyncConnection[Any]) -> int:
    """Put back checks whose lease ran out: their worker died or stalled (invariant 3)."""
    cursor = await conn.execute(
        "UPDATE checks SET state = 'queued', lease_id = NULL, lease_expires_at = NULL,"
        " updated_at = clock_timestamp()"
        " WHERE id IN (SELECT id FROM checks WHERE state = 'leased'"
        "   AND lease_expires_at <= clock_timestamp() FOR UPDATE SKIP LOCKED)"
        " RETURNING id"
    )
    expired = [row[0] for row in await cursor.fetchall()]
    if expired:
        await conn.execute(
            "UPDATE attempts SET outcome_class = 'lease_expired'"
            " WHERE check_id = ANY(%s) AND finished_at IS NULL AND outcome_class IS NULL",
            (expired,),
        )
    return len(expired)


async def claim(
    conn: AsyncConnection[Any], settings: WorkerSettings
) -> Claim | Wait | Idle | Exhausted:
    """Take capacity and a check together, so a leased check never waits for the limiter."""
    async with conn.transaction():
        bucket = await limiter.lock(conn, settings.payer_api)
        # After the lock: time spent queueing for it doesn't make the grant old.
        granted_at = time.monotonic()
        await reap(conn)
        if bucket.tokens < 1:
            return Wait((1 - bucket.tokens) / bucket.rate)
        if await limiter.in_flight(conn, bucket) >= bucket.max_in_flight:
            return Wait(1 / bucket.rate)

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, batch_id, cycle, member_id, given, family, birth_date, payer,"
                " service_date, traceparent FROM checks WHERE state = 'queued' AND not_before <= %s"
                " ORDER BY not_before LIMIT 1 FOR UPDATE SKIP LOCKED",
                (bucket.now,),
            )
            check = await cur.fetchone()
            if check is None:
                return Idle()
            await cur.execute(
                "SELECT coalesce(max(number), 0) AS last,"
                " count(*) FILTER (WHERE cycle = %(cycle)s"
                "   AND NOT coalesce(outcome_class = ANY(%(uncounted)s), false)) AS used,"
                " count(*) FILTER (WHERE cycle = %(cycle)s"
                "   AND outcome_class = 'malformed') AS malformed"
                " FROM attempts WHERE check_id = %(id)s",
                {"cycle": check["cycle"], "uncounted": UNCOUNTED, "id": check["id"]},
            )
            past = await cur.fetchone()
        assert past is not None  # noqa: S101 - aggregates always return a row
        request = CheckRequest.model_validate(
            {name: check[name] for name in CheckRequest.model_fields}
        )

        if past["used"] >= settings.max_attempts:
            await _give_up(conn, check["id"], request, "payer_unavailable", bucket.now)
            return Exhausted(check["id"], check["traceparent"])

        attempt_id, lease_id = uuid4(), uuid4()
        await conn.execute(
            "UPDATE checks SET state = 'leased', lease_id = %s, lease_expires_at = %s,"
            " updated_at = %s WHERE id = %s",
            (
                lease_id,
                bucket.now + timedelta(seconds=settings.lease_seconds),
                bucket.now,
                check["id"],
            ),
        )
        # Written before the call, so every call the payer could have had is on record.
        await conn.execute(
            "INSERT INTO attempts (id, check_id, number, cycle, started_at)"
            " VALUES (%s, %s, %s, %s, %s)",
            (attempt_id, check["id"], past["last"] + 1, check["cycle"], bucket.now),
        )
        await conn.execute(
            "UPDATE limiter_buckets SET tokens = %s, refilled_at = %s WHERE payer_api = %s",
            (bucket.tokens - 1, bucket.now, bucket.payer_api),
        )
        await conn.execute(
            "INSERT INTO limiter_slots (id, payer_api, expires_at) VALUES (%s, %s, %s)",
            (attempt_id, bucket.payer_api, bucket.now + timedelta(seconds=settings.slot_seconds)),
        )
    return Claim(
        check_id=check["id"],
        batch_id=check["batch_id"],
        attempt_id=attempt_id,
        lease_id=lease_id,
        key=f"{check['id']}:{check['cycle']}",
        request=request,
        attempts=past["used"] + 1,
        malformed_before=past["malformed"],
        granted_at=granted_at,
        traceparent=check["traceparent"],
    )


async def _give_up(
    conn: AsyncConnection[Any],
    check_id: UUID,
    request: CheckRequest,
    reason: str,
    now: datetime,
) -> None:
    await conn.execute(
        "UPDATE checks SET state = 'exception', exception_reason = %s, result = %s,"
        " lease_id = NULL, lease_expires_at = NULL, updated_at = %s WHERE id = %s",
        (reason, Jsonb(fhir.failed(check_id, request, reason, now)), now, check_id),
    )


async def finish(
    conn: AsyncConnection[Any],
    claimed: Claim,
    decision: Decision,
    *,
    attempt_class: str,
    http_status: int | None = None,
    body: str | None = None,
    delay_seconds: float = 0,
    release_slot: bool = True,
) -> bool:
    """Record an attempt, and move its check on if this claim still holds it.

    The result and the final state commit together. Returns False for a late reply: the
    lease was lost, so the attempt is recorded and the check is left alone (invariant 4).
    """
    now = datetime.now(UTC)
    async with conn.transaction():
        cursor = await conn.execute(
            "SELECT 1 FROM checks WHERE id = %s AND state = 'leased' AND lease_id = %s FOR UPDATE",
            (claimed.check_id, claimed.lease_id),
        )
        holds = await cursor.fetchone() is not None
        if holds:
            match decision:
                case Done(outcome, reply):
                    result = fhir.answered(claimed.check_id, claimed.request, reply, now)
                    await conn.execute(
                        "UPDATE checks SET state = 'done', outcome = %s, result = %s,"
                        " lease_id = NULL, lease_expires_at = NULL,"
                        " updated_at = clock_timestamp() WHERE id = %s",
                        (outcome, Jsonb(result), claimed.check_id),
                    )
                case GiveUp(reason):
                    await _give_up(conn, claimed.check_id, claimed.request, reason, now)
                case Retry():
                    await conn.execute(
                        "UPDATE checks SET state = 'queued', lease_id = NULL,"
                        " lease_expires_at = NULL, updated_at = clock_timestamp(),"
                        " not_before = clock_timestamp() + %s WHERE id = %s",
                        (timedelta(seconds=delay_seconds), claimed.check_id),
                    )
        await conn.execute(
            "UPDATE attempts SET finished_at = clock_timestamp(), outcome_class = %s,"
            " http_status = %s, response_body = %s WHERE id = %s",
            (
                attempt_class if holds else f"late:{attempt_class}",
                http_status,
                None if body is None else body[:MAX_KEPT_BODY].replace("\x00", ""),
                claimed.attempt_id,
            ),
        )
        if release_slot:
            await conn.execute("DELETE FROM limiter_slots WHERE id = %s", (claimed.attempt_id,))
    return holds
