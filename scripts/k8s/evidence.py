"""Check that a chaos run really disturbed what it meant to. Piped to a worker pod.

A run in which nothing was killed or degraded would pass the invariant report too, so it
isn't evidence of anything. Arguments: batch id, and the start and end of the degraded spell
in seconds since 1970.
"""

from __future__ import annotations

import os
import sys

import httpx2
import psycopg

from coverage_check.report import payer_log


def main(batch_id: str, degraded_from: float, degraded_until: float) -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        row = conn.execute(
            "SELECT count(*) FILTER (WHERE a.outcome_class = 'lease_expired'"
            "                        OR a.outcome_class LIKE 'late:%%'),"
            " count(*) FILTER (WHERE c.exception_reason = 'payer_unavailable')"
            " FROM attempts a JOIN checks c ON c.id = a.check_id WHERE c.batch_id = %s",
            (batch_id,),
        ).fetchone()
    assert row is not None  # noqa: S101 - aggregates always return a row
    expired, _ = row
    with httpx2.Client(base_url=os.environ["WORKER_PAYER_URL"], timeout=30) as client:
        during = [
            e for e in payer_log(client) if degraded_from <= e["received_at"] <= degraded_until
        ]
    bad = sum(e["behaviour"] in ("server_error", "throttle") for e in during)
    print(f"attempts whose lease ran out: {expired}")
    print(f"requests while degraded: {len(during)}, of them errors or throttled: {bad}")
    if not expired:
        sys.exit("No lease expired: the killed worker wasn't holding anything.")
    # The degraded profile fails half of requests; the normal one about one in twenty.
    if len(during) < 20 or bad / len(during) < 0.25:
        sys.exit("The payer was not visibly degraded.")


if __name__ == "__main__":
    main(sys.argv[1], float(sys.argv[2]), float(sys.argv[3]))
