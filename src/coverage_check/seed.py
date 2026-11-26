"""A sample batch from the synthetic-cohort members (ADR 0008)."""

from __future__ import annotations

from datetime import date
from itertools import cycle, islice

import synthetic_cohort

from coverage_check.models import MAX_CHECKS, BatchRequest, CheckRequest


def reference_date() -> date:
    """The day the members' coverage states are true for."""
    return date.fromisoformat(synthetic_cohort.manifest()["generation"]["reference_date"])


def sample_batch(count: int | None = None) -> BatchRequest:
    """`count` checks (by default one per member), going round the members in order.

    Only what a caller would know is copied. coverage_state is the expected answer, so it
    stays out of the request.
    """
    members = synthetic_cohort.members()
    count = len(members) if count is None else count
    if not 1 <= count <= MAX_CHECKS:
        raise ValueError(f"count must be from 1 to {MAX_CHECKS}")
    service_date = reference_date()
    return BatchRequest(
        checks=[
            CheckRequest(
                member_id=m["member_id"],
                given=m["given"],
                family=m["family"],
                birth_date=date.fromisoformat(m["birth_date"]),
                payer=m["payer"],
                service_date=service_date,
            )
            for m in islice(cycle(members), count)
        ]
    )
