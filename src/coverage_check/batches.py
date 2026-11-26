"""The SQL for batches and checks (ADR 0004)."""

from __future__ import annotations

import hashlib
from typing import Any, get_args
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import class_row, dict_row

from coverage_check.models import (
    Batch,
    BatchRequest,
    Check,
    CheckState,
    CheckSummary,
    Outcome,
)


class IdempotencyConflictError(Exception):
    """The key was already used for a different batch."""


def request_digest(request: BatchRequest) -> bytes:
    """Hash of the validated request, so JSON spacing and key order don't matter."""
    return hashlib.sha256(request.model_dump_json().encode()).digest()


async def submit_batch(
    conn: AsyncConnection[Any], key: str, request: BatchRequest
) -> tuple[UUID, bool]:
    """Write the batch and its checks in one transaction, once per key.

    Returns the batch id and whether this call created it. A concurrent submit with the same
    key waits on the unique index until the first one commits, then sees its row.
    """
    digest = request_digest(request)
    batch_id = uuid4()
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO batches (id, idempotency_key, request_sha256) VALUES (%s, %s, %s)"
            " ON CONFLICT (idempotency_key) DO NOTHING",
            (batch_id, key, digest),
        )
        if cur.rowcount == 0:
            await cur.execute(
                "SELECT id, request_sha256 FROM batches WHERE idempotency_key = %s", (key,)
            )
            row = await cur.fetchone()
            assert row is not None  # noqa: S101 - batches are never deleted
            existing_id, existing_digest = row
            if bytes(existing_digest) != digest:
                raise IdempotencyConflictError
            return existing_id, False
        await cur.executemany(
            "INSERT INTO checks (id, batch_id, position, member_id, given, family,"
            " birth_date, payer, service_date)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    uuid4(),
                    batch_id,
                    position,
                    c.member_id,
                    c.given,
                    c.family,
                    c.birth_date,
                    c.payer,
                    c.service_date,
                )
                for position, c in enumerate(request.checks)
            ],
        )
    return batch_id, True


async def get_batch(conn: AsyncConnection[Any], batch_id: UUID) -> Batch | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT created_at FROM batches WHERE id = %s", (batch_id,))
        batch = await cur.fetchone()
        if batch is None:
            return None
        # One statement, so the counts and the list always agree.
        await cur.execute(
            "SELECT id, position, state, outcome FROM checks WHERE batch_id = %s ORDER BY position",
            (batch_id,),
        )
        checks = [CheckSummary.model_validate(row) for row in await cur.fetchall()]
    states: dict[CheckState, int] = {state: 0 for state in get_args(CheckState)}
    outcomes: dict[Outcome, int] = {outcome: 0 for outcome in get_args(Outcome)}
    for check in checks:
        states[check.state] += 1
        if check.outcome is not None:
            outcomes[check.outcome] += 1
    return Batch(
        id=batch_id,
        created_at=batch["created_at"],
        total=len(checks),
        done=states["queued"] == 0 and states["leased"] == 0,
        states=states,
        outcomes=outcomes,
        checks=checks,
    )


_CHECK_COLUMNS = (
    "id, batch_id, position, member_id, given, family, birth_date, payer, service_date,"
    " state, outcome, exception_reason, result"
)


async def get_check(conn: AsyncConnection[Any], check_id: UUID) -> Check | None:
    async with conn.cursor(row_factory=class_row(Check)) as cur:
        await cur.execute(f"SELECT {_CHECK_COLUMNS} FROM checks WHERE id = %s", (check_id,))  # noqa: S608
        return await cur.fetchone()


async def list_exceptions(conn: AsyncConnection[Any], limit: int) -> list[Check]:
    """Oldest first: the order someone would work through them."""
    async with conn.cursor(row_factory=class_row(Check)) as cur:
        await cur.execute(
            f"SELECT {_CHECK_COLUMNS} FROM checks WHERE state = 'exception'"  # noqa: S608
            " ORDER BY updated_at, id LIMIT %s",
            (limit,),
        )
        return await cur.fetchall()
