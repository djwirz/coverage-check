"""The invariant report: what the database and the payer's own log say about a batch (spec §6)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx2
import psycopg
from psycopg.rows import dict_row
from pydantic import ValidationError

from coverage_check.fhir import CoverageEligibilityResponse
from coverage_check.queue import UNCOUNTED

# Requests the payer turned away at the door were never being worked on.
REFUSED = {"rate_limit", "in_flight_limit"}


@dataclass(frozen=True)
class Finding:
    invariant: int
    passed: bool
    detail: str


def peak_per_second(received: Sequence[float]) -> int:
    """The most requests in any one-second window."""
    times = sorted(received)
    peak = start = 0
    for end, at in enumerate(times):
        while times[start] <= at - 1:
            start += 1
        peak = max(peak, end - start + 1)
    return peak


def peak_in_flight(intervals: Sequence[tuple[float, float | None]]) -> int:
    """The most at once. A missing end means still being handled."""
    events = [(start, 1) for start, _ in intervals]
    events += [(end, -1) for _, end in intervals if end is not None]
    current = peak = 0
    # At the same instant, one leaving makes room before one arriving.
    for _, change in sorted(events):
        current += change
        peak = max(peak, current)
    return peak


def payer_log(client: httpx2.Client) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    after = 0
    while page := client.get("/requests", params={"after": after, "limit": 10_000}).json()[
        "requests"
    ]:
        entries.extend(page)
        after = page[-1]["id"]
    return entries


def _valid(result: Any) -> bool:
    try:
        CoverageEligibilityResponse.model_validate(result)
    except ValidationError:
        return False
    return True


def build(database_url: str, payer_url: str, batch_id: UUID, max_attempts: int) -> list[Finding]:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        batch = conn.execute("SELECT created_at FROM batches WHERE id = %s", (batch_id,)).fetchone()
        if batch is None:
            raise LookupError(f"no batch {batch_id}")
        checks = conn.execute(
            "SELECT id, cycle, state, outcome, exception_reason, result FROM checks"
            " WHERE batch_id = %s",
            (batch_id,),
        ).fetchall()
        attempts = conn.execute(
            "SELECT a.check_id, a.cycle, count(*) AS n FROM attempts a"
            " JOIN checks c ON c.id = a.check_id"
            " WHERE c.batch_id = %s AND NOT coalesce(a.outcome_class = ANY(%s), false)"
            " GROUP BY a.check_id, a.cycle",
            (batch_id, UNCOUNTED),
        ).fetchall()
    with httpx2.Client(base_url=payer_url, timeout=30) as client:
        limits = client.get("/limits").json()
        log = payer_log(client)

    # The limits are the account's, so everything the payer got since the batch began counts.
    since = batch["created_at"].timestamp() - 1
    log = [e for e in log if e["received_at"] >= since]
    rate = peak_per_second([e["received_at"] for e in log])
    flying = peak_in_flight(
        [(e["received_at"], e["replied_at"]) for e in log if e["behaviour"] not in REFUSED]
    )
    ids = {str(c["id"]) for c in checks}
    ours = [e for e in log if (e["idempotency_key"] or "").split(":")[0] in ids]
    billed = Counter(e["idempotency_key"] for e in ours if e["billable"])
    refused = sum(e["behaviour"] in REFUSED for e in log)

    states = Counter(c["state"] for c in checks)
    final = [c for c in checks if c["state"] in ("done", "exception")]
    unfinished = len(checks) - len(final)
    no_result = sum(c["result"] is None for c in final)
    over_cap = sum(a["n"] > max_attempts for a in attempts)
    no_reason = sum(c["state"] == "exception" and not c["exception_reason"] for c in checks)
    invalid = sum(not _valid(c["result"]) for c in final)
    twice = sum(n > 1 for n in billed.values())

    span = max((e["received_at"] for e in ours), default=0) - min(
        (e["received_at"] for e in ours), default=0
    )
    findings = [
        Finding(
            1,
            rate <= limits["rate_limit_per_second"]
            and flying <= limits["max_in_flight"]
            and refused == 0,
            f"peak {rate} requests in a second (limit {limits['rate_limit_per_second']}),"
            f" peak {flying} in flight (cap {limits['max_in_flight']}),"
            f" {refused} refused for being over",
        ),
        Finding(
            2,
            unfinished == 0 and no_result == 0,
            f"{len(final)} of {len(checks)} checks final"
            f" ({states['done']} done, {states['exception']} exceptions),"
            f" {no_result} without a result",
        ),
        Finding(
            3,
            states["queued"] == 0 and states["leased"] == 0,
            f"{states['queued']} queued, {states['leased']} leased",
        ),
        Finding(4, twice == 0, f"{len(billed)} keys billed, {twice} billed more than once"),
        Finding(
            5,
            over_cap == 0 and no_reason == 0,
            f"{over_cap} checks over {max_attempts} attempts,"
            f" {no_reason} exceptions without a reason",
        ),
        Finding(6, invalid == 0, f"{invalid} results that are not valid"),
    ]
    if ours and span > 0:
        findings.append(
            Finding(
                0, True, f"{len(ours)} payer requests over {span:.0f} s, {len(ours) / span:.1f}/s"
            )
        )
    return findings


def render(findings: Sequence[Finding]) -> str:
    lines = [
        f"{'PASS' if f.passed else 'FAIL'}  invariant {f.invariant}: {f.detail}"
        if f.invariant
        else f"      pace: {f.detail}"
        for f in findings
    ]
    return "\n".join(lines) + "\n"
